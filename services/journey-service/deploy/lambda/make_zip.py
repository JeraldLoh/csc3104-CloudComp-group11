"""Builds the disruption Lambda's zip. Runs inside the Linux container started by build.ps1:

    python make_zip.py <service dir> <staging dir that already holds the pip-installed packages> <output zip>

The Lambda needs only these files from app/ (the handler imports nothing else of ours, which is also
checked by a test in tests/test_disruption.py), plus httpx and redis. boto3 is already in the Lambda runtime.
"""
import shutil
import sys
import zipfile
from pathlib import Path

APP_FILES = ["__init__.py", "config.py", "cache.py", "lta.py", "disruption.py", "lambda_handler.py", "aws_resources.py",
             "data_mode.py"]
SKIP_DIRS = {"__pycache__", "bin"}


def main(service_dir: str, staging: str, out_zip: str) -> None:
    service, build = Path(service_dir), Path(staging)
    (build / "app").mkdir(parents=True, exist_ok=True)
    for name in APP_FILES:
        shutil.copy(service / "app" / name, build / "app" / name)

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(build.rglob("*")):
            rel = path.relative_to(build)
            if path.is_dir() or SKIP_DIRS & set(rel.parts) or path.suffix == ".pyc":
                continue
            z.write(path, rel.as_posix())          # forward slashes: the Lambda runs on Linux
    size_mb = Path(out_zip).stat().st_size / 1_000_000
    print(f"wrote {out_zip}: {size_mb:.1f} MB (Lambda's limit is 50 MB zipped, 250 MB unzipped)")


if __name__ == "__main__":
    main(*sys.argv[1:4])
