"""Minimal Wherobots job: count buildings within a buffer, then report back
to Step Functions via the callback pattern.

Invoked by the pipeline with:
    --task-token <token> --callback-url <relay function URL>
(and optionally --fail 1 to exercise the failure path). Run with no args,
it behaves as a plain standalone job.

Failure-reporting contract — three layers, because each catches what the
previous one cannot:
  1. try/except: any Python-level failure (bad SQL, bad data) posts a
     `failure` callback with the traceback. Step Functions fails the task
     immediately with that cause.
  2. heartbeats: a daemon thread posts a heartbeat every 60s. If this
     process dies where no except/finally can run (driver OOM, cluster
     kill), heartbeats simply stop and the state machine's
     HeartbeatSeconds timeout fires — the un-catchable, caught.
  3. finally: used ONLY to stop the heartbeat thread. Never post results
     from a finally — it runs after success too, and a hard death skips
     it anyway.
"""

import json
import sys
import threading
import traceback
import urllib.request


def parse_opts(argv):
    opts = {}
    i = 1
    while i < len(argv):
        if argv[i].startswith("--"):
            # A flag followed by a value consumes it; a trailing or bare flag
            # (no value, or another flag next) is recorded as "1" instead of
            # being silently dropped.
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[argv[i]] = argv[i + 1]
                i += 2
            else:
                opts[argv[i]] = "1"
                i += 1
        else:
            i += 1
    return opts


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def run_job():
    from sedona.spark import SedonaContext

    config = SedonaContext.builder().getOrCreate()
    sedona = SedonaContext.create(config)

    # Count Overture buildings within 1 km (geodesic, meters; EPSG:4326
    # lon/lat inputs) of downtown Seattle.
    df = sedona.sql(
        """
        SELECT COUNT(*) AS building_count
        FROM wherobots_open_data.overture_maps_foundation.buildings_building
        WHERE ST_Intersects(
            geometry,
            ST_Buffer(ST_Point(-122.335, 47.608), 1000.0, true)
        )
        """
    )
    count = int(df.first()["building_count"])
    print(f"Buildings within 1 km of downtown Seattle: {count}")
    return {"building_count": count}


def main():
    opts = parse_opts(sys.argv)
    url, token = opts.get("--callback-url"), opts.get("--task-token")
    callback = bool(url and token)
    stop_heartbeats = threading.Event()

    if callback:
        def beat():
            while not stop_heartbeats.wait(60):
                try:
                    post(url, {"task_token": token, "action": "heartbeat"})
                except Exception:
                    pass  # a missed heartbeat is fine; a stopped process is the signal

        threading.Thread(target=beat, daemon=True).start()

    try:
        if opts.get("--hard-fail"):
            # Simulate a driver OOM / SIGKILL: die instantly with no callback
            # and no goodbye. os._exit() bypasses except and finally entirely,
            # and the heartbeat thread dies with the process — the state
            # machine's HeartbeatSeconds timeout is the only thing that can
            # notice, which is exactly the point of this test mode.
            import os
            print("hello_wherobots_job: simulating hard death — no callback will be sent")
            os._exit(137)
        if opts.get("--fail"):
            raise RuntimeError("forced failure (--fail) to exercise the failure callback")
        output = run_job()
    except Exception as exc:
        if callback:
            try:
                post(url, {
                    "task_token": token,
                    "action": "failure",
                    "error": type(exc).__name__,
                    "cause": traceback.format_exc()[-4000:],
                })
            except Exception:
                pass  # relay unreachable — the heartbeat timeout is the backstop
        raise
    else:
        # Success delivery lives OUTSIDE the work try: a transient relay error
        # here must never be re-reported as a job failure. If the post fails,
        # exit successfully anyway — heartbeats stop with the process, the
        # heartbeat timeout fires, and the fallback poller observes the run's
        # real COMPLETED status.
        if callback:
            try:
                post(url, {"task_token": token, "action": "success", "output": output})
            except Exception:
                print("hello_wherobots_job: work succeeded but the success callback "
                      "could not be delivered — the fallback poller will report "
                      "COMPLETED", file=sys.stderr)
        print("hello_wherobots_job: SUCCESS")
    finally:
        stop_heartbeats.set()


if __name__ == "__main__":
    main()
