#!/usr/bin/env bash
# Build and deploy the Stage 1 payment-bot worker. The bash twin of deploy.ps1 — same
# steps, same stack, same parameters file — for CI (§3.6) and non-Windows hosts.
#
# Usage:
#   ./deploy/deploy.sh                          # deploy prod, schedule still disabled
#   ./deploy/deploy.sh --env staging            # deploy the staging stack
#   ./deploy/deploy.sh --plan                   # changeset only, execute nothing
#   ./deploy/deploy.sh --package-only           # build the zip, touch no AWS
#
# Requires only the AWS CLI and Python 3.11+. No Docker, no SAM.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$REPO_ROOT/deploy"

ENV_NAME="prod"
PARAMS_FILE=""
REGION="${AWS_REGION:-}"
PROFILE=""
CODE_BUCKET=""
PLAN=0
PACKAGE_ONLY=0
SKIP_BUILD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)          ENV_NAME="$2"; shift 2 ;;
    --params-file)  PARAMS_FILE="$2"; shift 2 ;;
    --region)       REGION="$2"; shift 2 ;;
    --profile)      PROFILE="$2"; shift 2 ;;
    --code-bucket)  CODE_BUCKET="$2"; shift 2 ;;
    --plan)         PLAN=1; shift ;;
    --package-only) PACKAGE_ONLY=1; shift ;;
    --skip-build)   SKIP_BUILD=1; shift ;;
    -h|--help)      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

STACK_NAME="paybot-${ENV_NAME}"
PYTHON="${PYTHON:-python3}"

AWS_ARGS=()
step() { printf '\n\033[36m=== %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
step "Preflight"
command -v aws >/dev/null || { echo "AWS CLI not found: https://aws.amazon.com/cli/" >&2; exit 1; }
command -v "$PYTHON" >/dev/null || { echo "$PYTHON not found (set PYTHON=...)" >&2; exit 1; }

if [[ -z "$REGION" ]]; then
  REGION="$(aws configure get region || true)"
  [[ -n "$REGION" ]] || REGION="us-east-1"
fi
AWS_ARGS=(--region "$REGION")
[[ -n "$PROFILE" ]] && AWS_ARGS+=(--profile "$PROFILE")

ACCOUNT="$(aws "${AWS_ARGS[@]}" sts get-caller-identity --query Account --output text)"
ok "aws cli   : $(aws --version 2>&1 | cut -d' ' -f1)"
ok "account   : $ACCOUNT"
ok "region    : $REGION"
ok "stack     : $STACK_NAME"

[[ -n "$PARAMS_FILE" ]] || PARAMS_FILE="$SCRIPT_DIR/params.${ENV_NAME}.json"
if [[ ! -f "$PARAMS_FILE" ]]; then
  cat >&2 <<EOF
Parameters file not found: $PARAMS_FILE

Create it from the example:
    cp deploy/params.example.json "$PARAMS_FILE"
then fill in the mailbox and Transport Pro values. No secrets go in it.
EOF
  exit 1
fi
ok "params    : $PARAMS_FILE"

[[ -n "$CODE_BUCKET" ]] || CODE_BUCKET="paybot-deploy-${ACCOUNT}-${REGION}"

# ---------------------------------------------------------------------------
# 2. Build
# ---------------------------------------------------------------------------
ZIP_PATH="$REPO_ROOT/dist/paybot-worker.zip"
if [[ $SKIP_BUILD -eq 1 ]]; then
  step "Build (skipped)"
  [[ -f "$ZIP_PATH" ]] || { echo "--skip-build given but $ZIP_PATH is missing" >&2; exit 1; }
  SHA="$($PYTHON -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()[:16])" "$ZIP_PATH")"
else
  step "Build"
  BUILD_OUT="$("$PYTHON" "$SCRIPT_DIR/build_package.py")"
  echo "$BUILD_OUT"
  SHA="$(echo "$BUILD_OUT" | grep '^SHA256=' | tail -1 | cut -d= -f2)"
  [[ -n "$SHA" ]] || { echo "build did not report a SHA256" >&2; exit 1; }
fi
CODE_KEY="paybot-worker/${SHA}.zip"
ok "code key  : $CODE_KEY"

if [[ $PACKAGE_ONLY -eq 1 ]]; then
  step "Done (--package-only)"
  echo "  $ZIP_PATH"
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. Artifact bucket — private, versioned, encrypted. Versioning is what makes a
#    rollback a redeploy of an older key rather than a rebuild of old source.
# ---------------------------------------------------------------------------
step "Artifact bucket"
if ! aws "${AWS_ARGS[@]}" s3api head-bucket --bucket "$CODE_BUCKET" >/dev/null 2>&1; then
  echo "  creating s3://$CODE_BUCKET"
  if [[ "$REGION" == "us-east-1" ]]; then
    # us-east-1 must NOT be given a LocationConstraint; the API rejects it.
    aws "${AWS_ARGS[@]}" s3api create-bucket --bucket "$CODE_BUCKET" >/dev/null
  else
    aws "${AWS_ARGS[@]}" s3api create-bucket --bucket "$CODE_BUCKET" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
  aws "${AWS_ARGS[@]}" s3api put-public-access-block --bucket "$CODE_BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null
  aws "${AWS_ARGS[@]}" s3api put-bucket-versioning --bucket "$CODE_BUCKET" \
    --versioning-configuration "Status=Enabled" >/dev/null
  aws "${AWS_ARGS[@]}" s3api put-bucket-encryption --bucket "$CODE_BUCKET" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
fi
ok "bucket    : s3://$CODE_BUCKET"

aws "${AWS_ARGS[@]}" s3 cp "$ZIP_PATH" "s3://$CODE_BUCKET/$CODE_KEY" --only-show-errors
ok "uploaded"

# ---------------------------------------------------------------------------
# 4. Parameters → the CLI's own JSON override form
# ---------------------------------------------------------------------------
step "Parameters"
OVERRIDES_FILE="$(mktemp)"
trap 'rm -f "$OVERRIDES_FILE"' EXIT
"$PYTHON" - "$PARAMS_FILE" "$ENV_NAME" "$CODE_BUCKET" "$CODE_KEY" > "$OVERRIDES_FILE" <<'PY'
import json, sys
params_file, env, bucket, key = sys.argv[1:5]
raw = json.load(open(params_file, encoding="utf-8"))
# Keys beginning with "_" are comments in the params file, not stack parameters.
out = [f"{k}={v}" for k, v in raw.items() if not k.startswith("_")]
out += [f"StackEnv={env}", f"CodeS3Bucket={bucket}", f"CodeS3Key={key}"]
json.dump(out, sys.stdout)
PY
ok "$(grep -o '=' <<<"$(cat "$OVERRIDES_FILE")" | wc -l | tr -d ' ') overrides"

# ---------------------------------------------------------------------------
# 5. Deploy
# ---------------------------------------------------------------------------
aws "${AWS_ARGS[@]}" cloudformation validate-template \
  --template-body "file://$SCRIPT_DIR/template.yaml" >/dev/null
ok "template validates"

DEPLOY_ARGS=(
  cloudformation deploy
  --template-file "$SCRIPT_DIR/template.yaml"
  --stack-name "$STACK_NAME"
  --parameter-overrides "file://$OVERRIDES_FILE"
  # NAMED_IAM: the execution role has a stable name, so a cross-account grant on the
  # CargoTel cookie bucket does not need re-pointing every time the stack is recreated.
  --capabilities CAPABILITY_NAMED_IAM
  --no-fail-on-empty-changeset
  --tags "app=payment-bot" "env=$ENV_NAME"
)
[[ $PLAN -eq 1 ]] && DEPLOY_ARGS+=(--no-execute-changeset)

if [[ $PLAN -eq 1 ]]; then step "Plan (nothing will be executed)"; else step "Deploy"; fi
if ! aws "${AWS_ARGS[@]}" "${DEPLOY_ARGS[@]}"; then
  cat >&2 <<EOF

  deploy failed — the most common causes, in order:
    * this identity cannot create IAM roles (an SSO read/analyst role usually cannot)
    * the stack is in ROLLBACK_COMPLETE and must be deleted before retrying:
        aws cloudformation delete-stack --stack-name $STACK_NAME
    * Bedrock model access is not enabled — see deploy/README.md step 1

  Full reason:
    aws cloudformation describe-stack-events --stack-name $STACK_NAME --max-items 15
EOF
  exit 1
fi

if [[ $PLAN -eq 1 ]]; then
  step "Plan complete"
  echo "  A changeset was created and NOT executed. Review it, then re-run without --plan."
  exit 0
fi

# ---------------------------------------------------------------------------
# 6. Report
# ---------------------------------------------------------------------------
step "Stack outputs"
aws "${AWS_ARGS[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output text |
  while IFS=$'\t' read -r k v; do printf '  %-22s %s\n' "$k" "$v"; done

FUNCTION_NAME="$(aws "${AWS_ARGS[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text)"

step "Next steps"
cat <<EOF
  1. Fill the two secrets — they deployed as placeholders. The stack owns their existence
     and IAM, never their values. See deploy/README.md step 4 for the exact commands.

  2. Smoke test, one email through the live path:
       aws lambda invoke --function-name $FUNCTION_NAME \\
         --payload '{"limit":1}' --cli-binary-format raw-in-base64-out response.json

  3. Enable the schedule only after diffing that run against the workstation log:
     set ScheduleEnabled to "true" in $PARAMS_FILE and re-run this script.
EOF
ok "done"
