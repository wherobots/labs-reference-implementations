#!/usr/bin/env python3
"""Local control-plane server for the reference dashboard.

Serves dashboard/index.html and exposes a small API the page uses to drive
the whole lifecycle. Credentials live in .env and are only ever injected
into subprocess environments on this machine — the browser never sees them
(preflight returns masked values only).

Endpoints:
  GET  /                      -> index.html
  GET  /api/preflight         -> .env / .state.json readiness report (masked)
  GET  /api/exec?script=NAME  -> Server-Sent Events stream of a lifecycle
                                 script's output (upload | deploy | teardown)
  POST /api/start             -> start a pipeline execution, returns its ARN
  GET  /api/execution?arn=ARN -> live digest: parent stage statuses, child
                                 poller progress, Wherobots run_id + status

Run:  python3 dashboard/server.py   (from the implementation root or dashboard/)
Then open http://127.0.0.1:8321
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
PORT = 8321

SCRIPTS = {
    "upload": ["python3", "scripts/01_upload_job.py"],
    "deploy": ["bash", "scripts/02_deploy.sh"],
    "teardown": ["bash", "scripts/99_teardown.sh"],
}

# files the read-only code viewer may serve (repo-relative)
VIEWABLE = {
    "job/hello_wherobots_job.py",
    "scripts/01_upload_job.py",
    "scripts/02_deploy.sh",
    "scripts/03_run.sh",
    "scripts/99_teardown.sh",
    "lambdas/submit/handler.py",
    "lambdas/check_status/handler.py",
    "lambdas/callback_relay/handler.py",
    "lambdas/find_run/handler.py",
    "statemachines/wherobots-pipeline.asl.json",
    "statemachines/wherobots-job-poller.asl.json",
}

_exec_lock = threading.Lock()  # one lifecycle script at a time


def load_env():
    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            if value:
                env[key.strip()] = value
    return env


def load_state():
    state_file = ROOT / ".state.json"
    return json.loads(state_file.read_text()) if state_file.exists() else {}


def subprocess_env():
    merged = dict(os.environ)
    merged.update(load_env())
    return merged


def mask(value):
    return f"…{value[-4:]}" if len(value) >= 8 else "set"


def aws(args):
    """Run an AWS CLI command with .env injected, return parsed JSON."""
    env = subprocess_env()
    region = env.get("AWS_REGION", "us-west-2")
    result = subprocess.run(
        ["aws", *args, "--region", region, "--output", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "aws cli failed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


# ── execution digest ─────────────────────────────────────────────────────────

PARENT_STAGES = ["PreviousStep", "SubmitAndAwaitCallback", "FindRun", "PollUntilComplete", "NextStep", "JobFailed"]


def execution_digest(arn):
    desc = aws(["stepfunctions", "describe-execution", "--execution-arn", arn])
    history = aws([
        "stepfunctions", "get-execution-history", "--execution-arn", arn,
        "--max-items", "500",
    ]).get("events", [])

    stages = {name: "pending" for name in PARENT_STAGES}
    stage_times = {}  # name -> {"entered": iso, "exited": iso|None} for the run Gantt
    child_arn = run_id = heartbeat_timeout_at = None
    for ev in history:
        etype = ev.get("type", "")
        if etype in ("TaskStateEntered", "PassStateEntered", "FailStateEntered"):
            name = ev["stateEnteredEventDetails"]["name"]
            if name in stages:
                # A Fail state ends the execution, so entering it IS failing.
                stages[name] = "failed" if etype == "FailStateEntered" else "active"
                stage_times.setdefault(name, {})["entered"] = str(ev.get("timestamp", ""))
        elif etype in ("TaskStateExited", "PassStateExited"):
            detail = ev["stateExitedEventDetails"]
            if detail["name"] in stages:
                stages[detail["name"]] = "succeeded"
                stage_times.setdefault(detail["name"], {})["exited"] = str(ev.get("timestamp", ""))
            if detail["name"] == "FindRun":
                try:
                    run_id = json.loads(detail.get("output", "{}")).get("found", {}).get("run_id")
                except (ValueError, AttributeError):
                    pass
        elif etype == "TaskTimedOut":
            # The heartbeat (or task) timeout firing — the moment silence
            # became the signal. Surfaced so the UI can demarcate it.
            heartbeat_timeout_at = str(ev.get("timestamp", ""))
        elif etype == "TaskSubmitted":
            try:
                out = json.loads(ev["taskSubmittedEventDetails"].get("output", "{}"))
                child_arn = out.get("ExecutionArn", child_arn)
            except (ValueError, KeyError):
                pass

    if heartbeat_timeout_at and stages.get("SubmitAndAwaitCallback") in ("active", "succeeded"):
        # The callback task ended by timeout, not by a callback — show it as
        # such rather than letting the Catch-exit read as success.
        stages["SubmitAndAwaitCallback"] = "timedout"

    if desc.get("status") in ("FAILED", "TIMED_OUT", "ABORTED"):
        for name, st in stages.items():
            if st == "active":
                stages[name] = "failed"

    poller = None
    if child_arn:
        poller = poller_digest(child_arn)
        if run_id is None and poller:
            run_id = poller.get("run_id")

    return {
        "status": desc.get("status"),
        "startDate": desc.get("startDate"),
        "stopDate": desc.get("stopDate"),
        "output": json.loads(desc["output"]) if desc.get("output") else None,
        "stages": stages,
        "stage_times": stage_times,
        "heartbeat_timeout_at": heartbeat_timeout_at,
        "run_id": run_id,
        "poller": poller,
    }




def poller_digest(child_arn):
    try:
        history = aws([
            "stepfunctions", "get-execution-history", "--execution-arn", child_arn,
            "--max-items", "500", "--reverse-order",
        ]).get("events", [])
    except RuntimeError:
        return {"execution_arn": child_arn}

    checks = sum(
        1 for ev in history
        if ev.get("type") == "TaskStateExited"
        and ev["stateExitedEventDetails"]["name"] == "CheckStatus"
    )
    latest = {}
    for ev in history:  # reverse order: first CheckStatus exit is the latest
        if (ev.get("type") == "TaskStateExited"
                and ev["stateExitedEventDetails"]["name"] == "CheckStatus"):
            try:
                latest = json.loads(ev["stateExitedEventDetails"].get("output", "{}")).get("check", {})
            except ValueError:
                pass
            break
    return {
        "execution_arn": child_arn,
        "checks": checks,
        "run_id": latest.get("run_id"),
        "wherobots_status": latest.get("status", "PENDING" if checks == 0 else None),
    }


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {fmt % args}")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            body = (ROOT / "dashboard" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/preflight":
            self.handle_preflight()
        elif url.path == "/api/exec":
            self.handle_exec(parse_qs(url.query).get("script", [""])[0])
        elif url.path == "/api/source":
            rel = parse_qs(url.query).get("file", [""])[0]
            if rel not in VIEWABLE:
                return self.send_json({"error": "file not viewable"}, 403)
            body = (ROOT / rel).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/execution":
            arn = parse_qs(url.query).get("arn", [""])[0]
            try:
                self.send_json(execution_digest(arn))
            except Exception as exc:  # surfaced to the UI, not fatal
                self.send_json({"error": str(exc)}, 500)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path == "/api/start":
            self.handle_start()
        else:
            self.send_json({"error": "not found"}, 404)

    def handle_preflight(self):
        env, state = load_env(), load_state()
        checks = {
            "env_file": (ROOT / ".env").exists(),
            "WHEROBOTS_API_KEY": mask(env["WHEROBOTS_API_KEY"]) if env.get("WHEROBOTS_API_KEY") else None,
            "WHEROBOTS_REGION": env.get("WHEROBOTS_REGION", "aws-us-west-2"),
            "AWS_REGION": env.get("AWS_REGION", "us-west-2"),
            "aws_credentials": "explicit (.env)" if env.get("AWS_ACCESS_KEY_ID")
                               else f"profile '{env['AWS_PROFILE']}'" if env.get("AWS_PROFILE")
                               else "default credential chain",
            "storage_target": f"Storage Integration '{env['STORAGE_INTEGRATION']}'"
                              if env.get("STORAGE_INTEGRATION") else "Wherobots managed storage",
            "runtime": env.get("JOB_RUNTIME", "tiny"),
            "uploaded": state.get("script_uri"),
            "deployed": state.get("pipeline_arn"),
        }
        self.send_json(checks)

    def handle_exec(self, script):
        if script not in SCRIPTS:
            return self.send_json({"error": f"unknown script '{script}'"}, 400)
        if not _exec_lock.acquire(blocking=False):
            return self.send_json({"error": "another script is already running"}, 409)
        proc = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            proc = subprocess.Popen(
                SCRIPTS[script], cwd=ROOT, env=subprocess_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in proc.stdout:
                self.sse(line.rstrip("\n"))
            proc.wait()
            self.sse(json.dumps({"exit_code": proc.returncode}), event="done")
        except BrokenPipeError:
            pass  # browser navigated away; the script keeps running to completion
        finally:
            # Hold the lock until the child actually exits: a closed browser
            # tab must not allow a second lifecycle script to run concurrently
            # with the one still finishing.
            if proc is not None:
                proc.wait()
            _exec_lock.release()

    def sse(self, data, event=None):
        if event:
            self.wfile.write(f"event: {event}\n".encode())
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

    def handle_start(self):
        env, state = load_env(), load_state()
        if not state.get("pipeline_arn") or not state.get("script_uri"):
            return self.send_json({"error": "upload + deploy must finish first"}, 400)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            body = {}
        job_name = "sfn-ref-" + time.strftime("%Y%m%d-%H%M%S")
        exec_input = json.dumps({
            "script": state["script_uri"],
            "job_name": job_name,
            "runtime": env.get("JOB_RUNTIME", "tiny"),
            "force_fail": bool(body.get("force_fail")),
            "hard_fail": bool(body.get("hard_fail")),
        })
        try:
            result = aws([
                "stepfunctions", "start-execution",
                "--state-machine-arn", state["pipeline_arn"],
                "--name", job_name,
                "--input", exec_input,
            ])
        except RuntimeError as exc:
            return self.send_json({"error": str(exc)}, 500)
        self.send_json({
            "execution_arn": result["executionArn"],
            "job_name": job_name,
            "input": json.loads(exec_input),
            "console_url": (
                f"https://{env.get('AWS_REGION', 'us-west-2')}.console.aws.amazon.com/states/home"
                f"?region={env.get('AWS_REGION', 'us-west-2')}"
                f"#/v2/executions/details/{result['executionArn']}"
            ),
        })


if __name__ == "__main__":
    os.chdir(ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard: http://127.0.0.1:{PORT}  (Ctrl-C to stop)")
    server.serve_forever()
