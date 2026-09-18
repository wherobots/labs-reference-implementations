#!/usr/bin/env python3
"""Step 1 — upload the job script to Wherobots storage.

Uploads job/hello_wherobots_job.py using the wherobots-python-sdk FilesAPI:
  - to a Storage Integration when STORAGE_INTEGRATION is set in .env
  - to Wherobots managed storage otherwise
Either way the SDK fetches short-lived STS credentials from the Wherobots
API for the upload — no long-lived AWS keys are involved.

Writes the resulting s3:// URI into .state.json for the later steps.

Usage: python3 scripts/01_upload_job.py   (from the implementation root)
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOB_FILE = ROOT / "job" / "hello_wherobots_job.py"
STATE_FILE = ROOT / ".state.json"
UPLOAD_SUBDIR = "job-scripts/sfn-reference"


def load_env(path: Path) -> dict:
    """Tiny .env parser: KEY=VALUE lines, '#' comments, blanks ignored."""
    env = {}
    if not path.exists():
        sys.exit(f"ERROR: {path} not found. Copy .env.example to .env and fill it in.")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def save_state(**updates):
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.update(updates)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def main():
    env = load_env(ROOT / ".env")
    if not env.get("WHEROBOTS_API_KEY"):
        sys.exit("ERROR: WHEROBOTS_API_KEY is not set in .env")

    from wherobots import FilesAPI
    from wherobots.config import WherobotsConfig

    config = WherobotsConfig.from_env(
        api_key=env["WHEROBOTS_API_KEY"],
        region=env.get("WHEROBOTS_REGION", "aws-us-west-2"),
    )

    integration = env.get("STORAGE_INTEGRATION", "")
    with FilesAPI.from_config(config) as files:
        if integration:
            print(f"Uploading {JOB_FILE.name} to Storage Integration '{integration}' ...")
            si = files.resolve_integration(integration)
            dest = files.dest_uri_for(si, JOB_FILE.name, UPLOAD_SUBDIR)
            script_uri = files.upload_file(str(JOB_FILE), dest)
        else:
            print(f"Uploading {JOB_FILE.name} to Wherobots managed storage ...")
            script_uri = files.upload_managed_file(str(JOB_FILE), UPLOAD_SUBDIR)

    print(f"Uploaded: {script_uri}")
    save_state(script_uri=script_uri)
    print(f"Saved script_uri to {STATE_FILE.name}")


if __name__ == "__main__":
    main()
