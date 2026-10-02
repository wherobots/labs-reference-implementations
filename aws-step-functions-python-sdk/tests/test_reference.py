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


class TestCallbackDelivery(unittest.TestCase):
    """The failure contract: a failed success-callback delivery must never be
    re-reported as a job failure (the fallback poller owns that case), while a
    failed job must post a failure callback with the traceback."""

    def setUp(self):
        self.posts = []
        self.orig_argv = sys.argv
        self.orig_post = hello_wherobots_job.post
        self.orig_run_job = hello_wherobots_job.run_job
        sys.argv = ["job", "--task-token", "tok", "--callback-url", "https://relay/cb"]

    def tearDown(self):
        sys.argv = self.orig_argv
        hello_wherobots_job.post = self.orig_post
        hello_wherobots_job.run_job = self.orig_run_job

    def test_success_delivery_failure_is_not_reported_as_job_failure(self):
        hello_wherobots_job.run_job = lambda: {"building_count": 1}

        def flaky_post(url, payload):
            self.posts.append(payload)
            if payload["action"] == "success":
                raise OSError("relay unreachable")

        hello_wherobots_job.post = flaky_post
        hello_wherobots_job.main()  # must not raise
        actions = [p["action"] for p in self.posts if p["action"] != "heartbeat"]
        self.assertEqual(actions, ["success"])
        self.assertNotIn("failure", actions)

    def test_job_failure_posts_failure_callback_with_traceback_and_reraises(self):
        def boom():
            raise RuntimeError("bad data")

        hello_wherobots_job.run_job = boom
        hello_wherobots_job.post = lambda url, payload: self.posts.append(payload)
        with self.assertRaises(RuntimeError):
            hello_wherobots_job.main()
        failures = [p for p in self.posts if p["action"] == "failure"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error"], "RuntimeError")
        self.assertIn("bad data", failures[0]["cause"])

    def test_happy_path_posts_exactly_one_success(self):
        hello_wherobots_job.run_job = lambda: {"building_count": 1084}
        hello_wherobots_job.post = lambda url, payload: self.posts.append(payload)
        hello_wherobots_job.main()
        actions = [p["action"] for p in self.posts if p["action"] != "heartbeat"]
        self.assertEqual(actions, ["success"])
        self.assertEqual(self.posts[-1]["output"], {"building_count": 1084})


class TestDashboardRequestGate(unittest.TestCase):
    """Regression tests for the cross-origin hardening: the dashboard must
    reject spoofed Hosts (DNS rebinding), foreign Origins, and any attempt to
    run a lifecycle script via GET or a non-JSON POST."""

    @classmethod
    def setUpClass(cls):
        import http.server
        sys.path.insert(0, str(IMPL / "dashboard"))
        import server as dashboard_server
        cls.mod = dashboard_server
        cls.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard_server.Handler)
        cls.port = cls.httpd.server_address[1]
        import threading
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        # The gate's allowlists are built from the canonical PORT constant;
        # widen them to this test server's ephemeral port.
        dashboard_server.ALLOWED_HOSTS.add(f"127.0.0.1:{cls.port}")
        dashboard_server.ALLOWED_ORIGINS.add(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _request(self, method, path, headers=None, body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_spoofed_host_is_rejected(self):
        status, _ = self._request(
            "GET", "/api/preflight", {"Host": "attacker.example:8321"})
        self.assertEqual(status, 403)

    def test_foreign_origin_is_rejected(self):
        status, _ = self._request(
            "GET", "/api/preflight",
            {"Host": f"127.0.0.1:{self.port}", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_same_origin_get_is_allowed(self):
        status, _ = self._request(
            "GET", "/api/preflight",
            {"Host": f"127.0.0.1:{self.port}",
             "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_exec_via_get_is_gone(self):
        status, _ = self._request(
            "GET", "/api/exec?script=teardown", {"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 404)

    def test_exec_post_requires_json_content_type(self):
        # A cross-site form/fetch can send text/plain without preflight —
        # it must be refused.
        status, _ = self._request(
            "POST", "/api/exec", {"Host": f"127.0.0.1:{self.port}",
                                  "Content-Type": "text/plain"},
            body='{"script":"teardown"}')
        self.assertEqual(status, 415)

    def test_json_null_body_gets_a_response_not_a_hang(self):
        # json.loads("null") is None; the handler must still answer with a 400
        # rather than treating it as already-responded and leaving the client
        # waiting on an HTTP/1.1 connection.
        status, _ = self._request(
            "POST", "/api/exec", {"Host": f"127.0.0.1:{self.port}",
                                  "Content-Type": "application/json"},
            body="null")
        self.assertEqual(status, 400)

    def test_exec_post_unknown_script_is_rejected(self):
        status, _ = self._request(
            "POST", "/api/exec", {"Host": f"127.0.0.1:{self.port}",
                                  "Content-Type": "application/json"},
            body='{"script":"rm -rf"}')
        self.assertEqual(status, 400)


class TestPinnedRequirements(unittest.TestCase):
    def test_sdk_version_is_pinned(self):
        req = (IMPL / "requirements.txt").read_text()
        for line in req.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                self.assertIn("==", line, f"unpinned requirement: {line}")


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
