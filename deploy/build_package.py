"""Build the Lambda deployment zip. Cross-platform, no Docker, no SAM.

Run directly or via ``deploy.ps1`` / ``deploy.sh``::

    python deploy/build_package.py --out dist/paybot-worker.zip

Two things here are load-bearing on Windows and are the reason this is a script rather
than three lines of shell:

**Wheels must be Linux ones.** ``pydantic-core`` is a compiled extension, so a plain
``pip install --target`` on Windows stages ``pydantic_core*.pyd`` — a DLL the Lambda
runtime cannot load, and the failure arrives at *import* time in the cloud rather than at
build time on the desk. ``--platform manylinux2014_x86_64 --only-binary=:all:`` forces the
Linux wheel and fails loudly here if one does not exist. Pure-Python dependencies are
unaffected: their ``py3-none-any`` wheels satisfy the same constraint.

**Zip entry names must use forward slashes, and files must be world-readable.** .NET's
``ZipFile.CreateFromDirectory`` on Windows PowerShell writes backslash-separated entries
that Lambda unpacks into files literally named ``payment_bot\\config.py``; and a zip built
on Windows carries no POSIX mode, which can land as an unreadable file. Python's
``zipfile`` gives control over both, so the zip is written here and never by the shell.

``boto3`` is deliberately NOT bundled — the Lambda Python runtime ships it, and vendoring
a second copy adds ~15 MB and a version that drifts from the one AWS patches.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Runtime dependencies, matching pyproject's `dependencies` plus the extras the deployed
#: worker needs. `google-auth` signs the service-account JWT; `beautifulsoup4` is the
#: `cargotel` extra, and the CargoTel client fails closed with an actionable error without
#: it — which would be a runtime discovery of a build mistake (§3.6).
REQUIREMENTS = (
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "google-auth>=2.28",
    "beautifulsoup4>=4.12",
)

#: Lambda's Python runtime. Must match `Runtime:` in template.yaml — a mismatch stages
#: wheels for the wrong interpreter and fails on import, not on build.
PYTHON_VERSION = "3.12"
PLATFORM = "manylinux2014_x86_64"

#: Dropped after install — dead weight in a package with a 250 MB unzipped limit and a cold
#: start to pay for.
#:
#: ``bin`` earns its place at the front. pip stages console-script launchers there, and on
#: Windows those are ``.exe`` stubs with the building machine's interpreter path baked in:
#: Windows binaries, shipped to a Linux runtime that will never invoke them, and different
#: on every build. They were the reason two identical builds produced different zips.
PRUNE_DIRS = ("bin", "Scripts", "__pycache__", "tests", "test")

#: Compared against the POSIX form of the path. Matching on ``str(path)`` silently did
#: nothing on Windows, where the separator is a backslash and ``.dist-info/RECORD`` never
#: matched ``.dist-info\RECORD`` — a prune that ran on every build and pruned nothing.
PRUNE_SUFFIXES = (".pyc", ".pyo", ".dist-info/RECORD", ".dist-info/INSTALLER")


def _run(command: list[str]) -> None:
    print("  $", " ".join(command))
    result = subprocess.run(command, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"failed ({result.returncode}): {' '.join(command)}")


def install_dependencies(build_dir: Path) -> None:
    """Stage Linux wheels for every runtime dependency into ``build_dir``."""

    print(f"[1/4] installing dependencies for {PLATFORM} / python{PYTHON_VERSION}")
    _run(
        [
            sys.executable, "-m", "pip", "install",
            "--target", str(build_dir),
            "--platform", PLATFORM,
            "--python-version", PYTHON_VERSION,
            "--implementation", "cp",
            # No source builds. A dependency without a Linux wheel must stop the build
            # here, where the message is legible, rather than produce a package that
            # imports a Windows binary in the cloud.
            "--only-binary=:all:",
            "--upgrade",
            "--no-compile",
            *REQUIREMENTS,
        ]
    )


def copy_source(build_dir: Path) -> None:
    """Copy the package itself. No wheel build: the source tree IS the artifact."""

    print("[2/4] copying payment_bot")
    source = REPO_ROOT / "src" / "payment_bot"
    if not source.is_dir():
        raise SystemExit(f"source package not found: {source}")
    shutil.copytree(
        source,
        build_dir / "payment_bot",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        dirs_exist_ok=True,
    )


def _size(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size


def prune(build_dir: Path) -> int:
    """Remove what the runtime will never import. Returns bytes freed.

    Deepest-first, so a directory is measured before an ancestor removes it.
    """

    print("[3/4] pruning")
    freed = 0
    for path in sorted(build_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.exists():
            continue  # already removed with its parent
        if path.is_dir():
            if path.name not in PRUNE_DIRS:
                continue
            freed += _size(path)
            shutil.rmtree(path, ignore_errors=True)
            continue
        if path.relative_to(build_dir).as_posix().endswith(PRUNE_SUFFIXES):
            freed += _size(path)
            path.unlink(missing_ok=True)
    return freed


def write_zip(build_dir: Path, out: Path) -> None:
    """Zip ``build_dir`` with POSIX-correct entry names and modes.

    Entries are sorted so an unchanged tree produces a byte-identical zip — which is what
    lets the caller key the S3 object on the content hash and skip a redundant upload.
    """

    print(f"[4/4] writing {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    file_mode = (stat.S_IFREG | 0o644) << 16
    dir_mode = (stat.S_IFDIR | 0o755) << 16
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(build_dir.rglob("*")):
            # as_posix(): Lambda unpacks entry names literally, so a backslash becomes part
            # of the filename rather than a directory level.
            name = path.relative_to(build_dir).as_posix()
            if path.is_dir():
                info = zipfile.ZipInfo(name + "/", date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = dir_mode
                archive.writestr(info, b"")
                continue
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = file_mode
            archive.writestr(info, path.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the payment-bot Lambda zip.")
    parser.add_argument("--out", default="dist/paybot-worker.zip")
    parser.add_argument("--build-dir", default="dist/build")
    parser.add_argument(
        "--keep-build-dir",
        action="store_true",
        help="leave the staging tree in place to inspect what was packaged",
    )
    args = parser.parse_args(argv)

    build_dir = (REPO_ROOT / args.build_dir).resolve()
    out = (REPO_ROOT / args.out).resolve()

    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    install_dependencies(build_dir)
    copy_source(build_dir)
    freed = prune(build_dir)
    write_zip(build_dir, out)
    if not args.keep_build_dir:
        shutil.rmtree(build_dir, ignore_errors=True)

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    size_mb = out.stat().st_size / 1_048_576
    print()
    print(f"  package : {out}")
    print(f"  size    : {size_mb:.1f} MB zipped  (pruned {freed / 1_048_576:.1f} MB)")
    print(f"  sha256  : {digest}")
    # Read by deploy.ps1/deploy.sh to key the S3 object, so an unchanged build does not
    # churn a new object version and an unchanged stack does not redeploy.
    print(f"SHA256={digest[:16]}")

    if size_mb > 50:
        print()
        print("  ! over Lambda's 50 MB direct-upload limit — the S3 path this script uses")
        print("    still works, but the unzipped 250 MB limit is the next one to watch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
