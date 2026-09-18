"""Unit tests for the pieces of this reference that can be tested without AWS
or Wherobots: the job's argument parsing, the structural invariants of the
state-machine definitions, and the dashboard's drain-before-wait subprocess
pattern (regression test for the exec-lock deadlock).

Run from the repo root:  python3 -m unittest discover -s aws-step-functions-python-sdk/tests
"""

import json
import pathlib
import subprocess
import sys
import time
import unittest

IMPL = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(IMPL / "job"))

import hello_wherobots_job  # noqa: E402  (stdlib-only at import time)


class TestParseOpts(unittest.TestCase):
    def test_flag_value_pairs(self):
        opts = hello_wherobots_job.parse_opts(
            ["prog", "--task-token", "tok123", "--callback-url", "https://x/cb"])
        self.assertEqual(opts, {"--task-token": "tok123", "--callback-url": "https://x/cb"})

    def test_trailing_bare_flag_is_recorded_not_dropped(self):
        opts = hello_wherobots_job.parse_opts(["prog", "--fail", "yes", "--hard-fail"])
        self.assertEqual(opts["--hard-fail"], "1")

    def test_adjacent_flags_record_first_as_bare(self):
        opts = hello_wherobots_job.parse_opts(["prog", "--hard-fail", "--fail", "yes"])
        self.assertEqual(opts, {"--hard-fail": "1", "--fail": "yes"})

    def test_non_flag_tokens_ignored(self):
        opts = hello_wherobots_job.parse_opts(["prog", "stray", "--k", "v", "stray2"])
        self.assertEqual(opts, {"--k": "v"})


class TestPipelineDefinition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.asl = json.loads((IMPL / "statemachines" / "wherobots-pipeline.asl.json").read_text())
        cls.states = cls.asl["States"]

    def test_every_transition_targets_an_existing_state(self):
        names = set(self.states)
        for name, state in self.states.items():
            targets = []
            if "Next" in state:
                targets.append(state["Next"])
            for c in state.get("Catch", []):
                targets.append(c["Next"])
            for missing in set(targets) - names:
                self.fail(f"{name} transitions to undefined state {missing!r}")
        self.assertIn(self.asl["StartAt"], names)

    def test_heartbeat_window_below_task_ceiling(self):
        submit = self.states["SubmitAndAwaitCallback"]
        self.assertLess(submit["HeartbeatSeconds"], submit["TimeoutSeconds"])

    def test_heartbeat_window_covers_job_heartbeat_interval(self):
        # The job beats every 60s; the window must comfortably exceed one
        # interval or a healthy running job would time out between beats.
        submit = self.states["SubmitAndAwaitCallback"]
        self.assertGreaterEqual(submit["HeartbeatSeconds"], 120)

    def test_timeout_routes_to_fallback_and_taskfailed_to_jobfailed(self):
        catches = self.states["SubmitAndAwaitCallback"]["Catch"]
        routed = {err: c["Next"] for c in catches for err in c["ErrorEquals"]}
        self.assertEqual(routed["States.Timeout"], "FindRun")
        self.assertEqual(routed["States.HeartbeatTimeout"], "FindRun")
        self.assertEqual(routed["States.TaskFailed"], "JobFailed")

    def test_jobfailed_surfaces_dynamic_error_not_static_strings(self):
        fail = self.states["JobFailed"]
        self.assertEqual(fail["ErrorPath"], "$.error.Error")
        self.assertEqual(fail["CausePath"], "$.error.Cause")
        self.assertNotIn("Error", fail)
        self.assertNotIn("Cause", fail)

    def test_every_catch_captures_the_error_where_jobfailed_reads_it(self):
        for name, state in self.states.items():
            for c in state.get("Catch", []):
                self.assertEqual(
                    c.get("ResultPath"), "$.error",
                    f"{name} Catch must write to $.error for JobFailed's ErrorPath/CausePath")


class TestPollerDefinition(unittest.TestCase):
    def test_poller_parses_and_transitions_resolve(self):
        asl = json.loads((IMPL / "statemachines" / "wherobots-job-poller.asl.json").read_text())
        names = set(asl["States"])
        self.assertIn(asl["StartAt"], names)
        for name, state in asl["States"].items():
            for target in ([state.get("Next")] +
                           [c.get("Next") for c in state.get("Choices", [])] +
                           [state.get("Default")]):
                if target is not None:
                    self.assertIn(target, names, f"{name} -> {target}")


class TestDrainBeforeWait(unittest.TestCase):
    def test_abandoned_reader_does_not_deadlock(self):
        """Regression test for the exec-lock deadlock: a child writing far past
        the OS pipe buffer must still be reap-able after the reader abandons the
        stream, provided stdout is drained to EOF before wait() — the pattern
        dashboard/server.py's handle_exec finally-block uses."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "for i in range(20000): print('x' * 100)"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(3):  # simulate the SSE loop dying after a few lines
            next(iter(proc.stdout))
        start = time.monotonic()
        for _ in proc.stdout:  # the drain
            pass
        proc.wait()
        proc.stdout.close()
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
