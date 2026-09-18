# Labs Reference Implementations

Minimal, self-contained reference implementations for integrating Wherobots
with common orchestration and cloud tooling. Each subdirectory is one
implementation with its own README, deploy scripts, guided dashboard, and
symmetric teardown.

These are learning tools, not production builds: they favor transparency
(every command visible, every failure mode demonstrable) over abstraction.

| Implementation | What it shows |
|----------------|---------------|
| [aws-step-functions-python-sdk](aws-step-functions-python-sdk/) | AWS Step Functions pipeline that submits a Wherobots job run via the `wherobots-python-sdk` using the callback pattern (`waitForTaskToken` + heartbeats), with a reusable poller state machine as the fallback for failures the job cannot report itself. Includes a guided browser dashboard driving the whole lifecycle: upload → deploy → run (with live block timeline and three test modes, hard-death OOM simulation included) → teardown. |

More implementations are on the way.

## Ground rules shared by every implementation

- Secrets live only in a local, gitignored `.env`; each directory ships a
  documented `.env.example`.
- Every AWS resource is created by readable CLI calls, tagged
  (`ManagedBy`, `Project`, `CreatedAt`, `TeardownBy`), and removed by a
  symmetric teardown script.
- Every number in a README comes from a verified live run.
