<#
.SYNOPSIS
    Build and deploy the Stage 1 payment-bot worker to AWS. No console clicks.

.DESCRIPTION
    Packages the Lambda (Linux wheels, cross-compiled from Windows), uploads it, and
    deploys deploy/template.yaml with CloudFormation. Idempotent: re-run it after any code
    or parameter change and only the difference is applied.

    Only the AWS CLI and Python are required — no Docker, no SAM CLI, no Node.

    Three things this script deliberately does NOT do, because they are not scriptable or
    not mine to do:
      * Enable Bedrock model access (an account-level opt-in; README.md step 1)
      * Put real credential values into Secrets Manager (it prints the exact commands)
      * Enable the schedule (deploy disabled, verify, then flip — cutover step 2)

.EXAMPLE
    .\deploy\deploy.ps1 -Plan
    Build, then show what would change without touching anything.

.EXAMPLE
    .\deploy\deploy.ps1
    Build and deploy to the prod stack, schedule still disabled.

.EXAMPLE
    .\deploy\deploy.ps1 -Env staging -ParamsFile deploy\params.staging.json
#>

[CmdletBinding()]
param(
    [ValidateSet("prod", "staging")]
    [string]$Env = "prod",

    # Defaults to deploy\params.<env>.json. Copy params.example.json to create it.
    [string]$ParamsFile = "",

    [string]$Region = "",
    [string]$AwsProfile = "",

    # Defaults to paybot-deploy-<account>-<region>. Created if absent, with public access
    # blocked and versioning on.
    [string]$CodeBucket = "",

    # Create the changeset and print it, execute nothing. The safe first run.
    [switch]$Plan,

    # Build the zip and stop. Nothing touches AWS.
    [switch]$PackageOnly,

    # Reuse dist\paybot-worker.zip from a previous run.
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$stackName = "paybot-$Env"

function Write-Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  ok  $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "  !   $text" -ForegroundColor Yellow }

# Every AWS call carries the same region/profile. Built once as an array so the values are
# passed as separate argv entries and never re-parsed out of a string.
$awsCommon = @()
if ($Region)     { $awsCommon += @("--region", $Region) }
if ($AwsProfile) { $awsCommon += @("--profile", $AwsProfile) }

# stderr goes to a temp FILE, never `2>&1`, and $ErrorActionPreference drops to Continue
# for the duration of the call. Windows PowerShell 5.1 wraps a native command's redirected
# stderr in ErrorRecords even when the target is a file — under the script's global
# "Stop" preference, the first stderr line aws writes (a routine "bucket not found", say)
# throws before the exit code is ever consulted. The local Continue keeps stderr flowing
# to the file and leaves $LASTEXITCODE as the single source of truth.
function Invoke-Aws {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $output = & aws @Arguments 2>$errFile
        if ($LASTEXITCODE -ne 0) {
            Write-Host (Get-Content $errFile -Raw) -ForegroundColor Red
            throw "aws $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
        }
        return $output
    } finally {
        Remove-Item $errFile -Force -ErrorAction SilentlyContinue
    }
}

# The same call when a non-zero exit is an answer rather than a failure ("does this bucket
# exist?"). Returns the exit code and swallows the output.
function Test-AwsSucceeds {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        & aws @Arguments 2>$errFile | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item $errFile -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# 1. Preflight. Fail here with a fixable message, not halfway through a deploy.
# ---------------------------------------------------------------------------
Write-Step "Preflight"

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "AWS CLI not found. Install it: https://aws.amazon.com/cli/"
}
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) { throw "Python not found. Need 3.11+ to build the package." }
Write-Ok "aws cli   : $((& aws --version 2>&1) -split ' ' | Select-Object -First 1)"
Write-Ok "python    : $python"

if (-not $Region) {
    # `aws configure get` exits 1 when the key is unset, which is an answer, not a failure.
    $Region = ""
    try { $Region = (& aws configure get region) } catch { $Region = "" }
    if (-not $Region) { $Region = "us-east-1" }
    $awsCommon = @("--region", $Region)
    if ($AwsProfile) { $awsCommon += @("--profile", $AwsProfile) }
}

$identity = (Invoke-Aws @awsCommon sts get-caller-identity --output json | Out-String | ConvertFrom-Json)
$account = $identity.Account
Write-Ok "account   : $account"
Write-Ok "region    : $Region"
Write-Ok "identity  : $($identity.Arn)"
Write-Ok "stack     : $stackName"

if (-not $ParamsFile) { $ParamsFile = Join-Path $PSScriptRoot "params.$Env.json" }
if (-not (Test-Path $ParamsFile)) {
    throw @"
Parameters file not found: $ParamsFile

Create it from the example:
    Copy-Item deploy\params.example.json $ParamsFile
then fill in the mailbox and Transport Pro values. No secrets go in it.
"@
}
Write-Ok "params    : $ParamsFile"

if (-not $CodeBucket) { $CodeBucket = "paybot-deploy-$account-$Region" }

# ---------------------------------------------------------------------------
# 2. Build.
# ---------------------------------------------------------------------------
$zipPath = Join-Path $repoRoot "dist\paybot-worker.zip"
if ($SkipBuild) {
    if (-not (Test-Path $zipPath)) { throw "-SkipBuild given but $zipPath does not exist." }
    Write-Step "Build (skipped)"
    $sha = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLower().Substring(0, 16)
} else {
    Write-Step "Build"
    # No `2>&1`: pip writes its progress to stderr, and capturing it would make an ordinary
    # download line a terminating error. It flows straight to the console instead.
    $buildOutput = & $python (Join-Path $PSScriptRoot "build_package.py")
    Write-Host ($buildOutput | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "package build failed" }
    $shaLine = $buildOutput | Select-String -Pattern "^SHA256=" | Select-Object -Last 1
    if (-not $shaLine) { throw "build did not report a SHA256" }
    $sha = ($shaLine -split "=")[1].Trim()
}
$codeKey = "paybot-worker/$sha.zip"
Write-Ok "code key  : $codeKey"

if ($PackageOnly) {
    Write-Step "Done (-PackageOnly)"
    Write-Host "  $zipPath"
    exit 0
}

# ---------------------------------------------------------------------------
# 3. Code bucket. Private and versioned — it holds every artifact ever deployed,
#    which is what makes a rollback a redeploy of an older key.
# ---------------------------------------------------------------------------
Write-Step "Artifact bucket"
$exists = Test-AwsSucceeds @awsCommon s3api head-bucket --bucket $CodeBucket

if (-not $exists) {
    Write-Host "  creating s3://$CodeBucket"
    if ($Region -eq "us-east-1") {
        # us-east-1 must NOT be given a LocationConstraint; the API rejects it.
        Invoke-Aws @awsCommon s3api create-bucket --bucket $CodeBucket | Out-Null
    } else {
        Invoke-Aws @awsCommon s3api create-bucket --bucket $CodeBucket `
            --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
    }
    Invoke-Aws @awsCommon s3api put-public-access-block --bucket $CodeBucket `
        --public-access-block-configuration `
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" | Out-Null
    Invoke-Aws @awsCommon s3api put-bucket-versioning --bucket $CodeBucket `
        --versioning-configuration "Status=Enabled" | Out-Null
    # Inline JSON loses its double quotes crossing the PS 5.1 native-argument boundary,
    # so the CLI receives {Rules:[...]} and rejects it. A file:// reference keeps the
    # payload out of the shell entirely. ASCII, not UTF8: Out-File's BOM breaks the parse.
    $sseFile = [System.IO.Path]::GetTempFileName()
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' |
        Set-Content -Path $sseFile -Encoding Ascii
    try {
        Invoke-Aws @awsCommon s3api put-bucket-encryption --bucket $CodeBucket `
            --server-side-encryption-configuration "file://$sseFile" | Out-Null
    } finally {
        Remove-Item $sseFile -Force -ErrorAction SilentlyContinue
    }
}
Write-Ok "bucket    : s3://$CodeBucket"

Write-Host "  uploading $codeKey"
Invoke-Aws @awsCommon s3 cp $zipPath "s3://$CodeBucket/$codeKey" --only-show-errors | Out-Null
Write-Ok "uploaded"

# ---------------------------------------------------------------------------
# 4. Parameters. Rendered to the CLI's own JSON form rather than passed as
#    Key=Value words: PowerShell 5.1's native-argument quoting mangles values
#    with spaces, and "Circle Delivers Payments" is one.
# ---------------------------------------------------------------------------
Write-Step "Parameters"
$raw = Get-Content $ParamsFile -Raw | ConvertFrom-Json
$overrides = New-Object System.Collections.Generic.List[string]
foreach ($property in $raw.PSObject.Properties) {
    if ($property.Name.StartsWith("_")) { continue }   # comment keys
    $overrides.Add("$($property.Name)=$($property.Value)")
}
$overrides.Add("StackEnv=$Env")
$overrides.Add("CodeS3Bucket=$CodeBucket")
$overrides.Add("CodeS3Key=$codeKey")

$overridesFile = Join-Path $env:TEMP "paybot-params-$Env.json"
# Ascii, not utf8: PS 5.1's utf8 writes a BOM, and the CLI's JSON parser rejects the file
# outright ("Expecting value: line 1 column 1"). Every parameter value here is ASCII.
$overrides | ConvertTo-Json | Set-Content -Path $overridesFile -Encoding Ascii
foreach ($o in $overrides) {
    if ($o -match "^(GmailUser|TransportProBaseUrl|FetchLimit|ScheduleEnabled|CargoTelReplies|Timezone|BedrockModelId)=") {
        Write-Host "  $o"
    }
}

# ---------------------------------------------------------------------------
# 5. Deploy.
# ---------------------------------------------------------------------------
$templatePath = Join-Path $PSScriptRoot "template.yaml"
Invoke-Aws @awsCommon cloudformation validate-template --template-body "file://$templatePath" | Out-Null
Write-Ok "template validates"

$deployArgs = @(
    "cloudformation", "deploy",
    "--template-file", $templatePath,
    "--stack-name", $stackName,
    "--parameter-overrides", "file://$overridesFile",
    # NAMED_IAM because the execution role is given a stable name — an unnamed role would
    # be replaced on every stack recreation and every cross-account grant re-pointed.
    "--capabilities", "CAPABILITY_NAMED_IAM",
    "--no-fail-on-empty-changeset",
    "--tags", "app=payment-bot", "env=$Env"
)
if ($Plan) { $deployArgs += "--no-execute-changeset" }

if ($Plan) { Write-Step "Plan (nothing will be executed)" } else { Write-Step "Deploy" }
# Continue, not Stop, around the one long-running call: `cloudformation deploy` writes
# progress and "No changes to deploy" to stderr, which the global Stop preference would
# turn fatal before the $LASTEXITCODE check below ever runs.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& aws @awsCommon @deployArgs
$ErrorActionPreference = $prevEap
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "deploy failed — the most common causes, in order:"
    Write-Host "    * this identity cannot create IAM roles (an SSO read/analyst role usually cannot)"
    Write-Host "    * the stack is in ROLLBACK_COMPLETE and must be deleted before retrying:"
    Write-Host "        aws cloudformation delete-stack --stack-name $stackName"
    Write-Host "    * Bedrock model access is not enabled — see deploy\README.md step 1"
    Write-Host "`n  Full reason:"
    Write-Host "    aws cloudformation describe-stack-events --stack-name $stackName --max-items 15"
    throw "cloudformation deploy failed"
}

if ($Plan) {
    Write-Step "Plan complete"
    Write-Host "  A changeset was created and NOT executed. Review it, then re-run without -Plan."
    exit 0
}

# ---------------------------------------------------------------------------
# 6. Report + the steps that are deliberately still manual.
# ---------------------------------------------------------------------------
Write-Step "Stack outputs"
$outputs = (Invoke-Aws @awsCommon cloudformation describe-stacks --stack-name $stackName `
        --query "Stacks[0].Outputs" --output json | Out-String | ConvertFrom-Json)
$out = @{}
foreach ($o in $outputs) { $out[$o.OutputKey] = $o.OutputValue; Write-Host ("  {0,-22} {1}" -f $o.OutputKey, $o.OutputValue) }

Write-Step "Next steps"
Write-Host @"
  1. Fill the two secrets. They deployed as placeholders — the stack owns their
     existence and IAM, never their values. Run these yourself:

       aws secretsmanager put-secret-value --secret-id $($out['GoogleSecretArn']) ``
         --secret-string file://path\to\service-account.json

       aws secretsmanager put-secret-value --secret-id $($out['TransportProSecretArn']) ``
         --secret-string 'THE-TP-PASSWORD'

     Until both are real the worker fails closed on its first Gmail call. That is the
     intended behaviour: a bot that starts without credentials and reports "no mail
     matched" is indistinguishable from a quiet inbox.

  2. Smoke test — one email through the live path, everything else untouched:

       aws lambda invoke --function-name $($out['FunctionName']) ``
         --payload '{\"limit\":1}' --cli-binary-format raw-in-base64-out response.json
       Get-Content response.json

  3. Watch it:

       aws logs tail $($out['LogGroup']) --follow

  4. Diff that run against the same hour's workstation log. Only then enable the
     schedule: set ScheduleEnabled to "true" in $ParamsFile and re-run this script.
     It is currently $($out['ScheduleState']).

  Rollback at any point: set ScheduleEnabled back to "false" and re-run, then re-enable
  the Windows task. The workstation setup stays intact until you delete it.
"@
Write-Ok "done"
