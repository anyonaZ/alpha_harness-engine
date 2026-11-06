"""A backend killed mid-send recovers without sending anything twice (Linux).

The plan's behaviour gate. Starts ``fake_brain.py`` with every send held open for
``--post-delay`` seconds after BRAIN has it, runs a Search Lab task, and kills the backend
with SIGKILL while a send is held: BRAIN is running that batch, and the backend never
learned its id. The backend is started again on the same data and runs for ``--after``
minutes, long enough for the stale-send sweep and the orphan pass to find the batch's
Alphas. Then it is stopped and checked:

- no simulation body reached BRAIN twice;
- every row the kill cut off was adopted (its Alpha found and recorded) or is still waiting
  within the orphan window, none queued again while its Alpha existed;
- the task kept running after the restart.

Exits non-zero on a failure. ``--kills`` repeats the kill and restart.

    backend/.venv/bin/python bench/recovery_check.py --backend backend
"""

# ruff: noqa: T201 - a check reports on stdout

import argparse
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from busy_bench import HERE, LAUNCHER, get, wait_for


def start_app(
    python: str, brain_url: str, port: int, env: dict[str, str], home: str
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        [python, "-c", LAUNCHER, brain_url, str(port)],
        cwd=home,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def counts(db: Path) -> dict[str, dict[str, int]]:
    with sqlite3.connect(db) as conn:
        sims = dict(conn.execute("SELECT status, count(*) FROM simulation_record GROUP BY status"))
        trials = dict(conn.execute("SELECT state, count(*) FROM trial GROUP BY state"))
    return {"simulations": sims, "trials": trials}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="backend")
    parser.add_argument("--sim-seconds", type=float, default=30.0)
    parser.add_argument("--post-delay", type=float, default=4.0)
    parser.add_argument("--before", type=float, default=1.5, help="minutes before the kill")
    parser.add_argument("--after", type=float, default=7.0, help="minutes after a restart")
    parser.add_argument("--kills", type=int, default=1)
    parser.add_argument("--seed-trials", type=int, default=2000)
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--brain-port", type=int, default=8900)
    args = parser.parse_args()
    backend = Path(args.backend).resolve()
    python = str(backend / ".venv" / "bin" / "python")
    app_url = f"http://127.0.0.1:{args.port}"
    brain_url = f"http://127.0.0.1:{args.brain_port}"
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home, "HARNESS_PROFILE": "balanced"}
        db = Path(home) / ".alpha-harness" / "harness.db"
        seed = [str(HERE / "busy_seed.py"), "--db", str(db)]
        seed += ["--trials", str(args.seed_trials), "--fields", "1500"]
        subprocess.run([python, *seed], env=env, cwd=home, check=True)  # noqa: S603
        brain = subprocess.Popen(  # noqa: S603
            [
                python,
                str(HERE / "fake_brain.py"),
                "--port",
                str(args.brain_port),
                "--sim-seconds",
                str(args.sim_seconds),
                "--post-delay",
                str(args.post_delay),
            ],
            cwd=home,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app = start_app(python, brain_url, args.port, env, home)
        try:
            wait_for(f"{brain_url}/__stats")
            wait_for(f"{app_url}/api/health")
            get(f"{app_url}/api/lab-tasks/run-all", method="POST")
            print(
                f"{backend.parent.name}: {args.sim_seconds:.0f} s simulations, "
                f"sends held {args.post_delay:.0f} s"
            )
            time.sleep(args.before * 60)
            for kill in range(1, args.kills + 1):
                # Kill while BRAIN holds a send the backend is waiting on.
                deadline = time.monotonic() + 120
                while get(f"{brain_url}/__stats")["posting"] == 0:
                    if time.monotonic() > deadline:
                        raise SystemExit("no send to interrupt in two minutes")
                    time.sleep(0.2)
                time.sleep(args.post_delay / 2)
                app.send_signal(signal.SIGKILL)
                app.wait()
                at_kill = get(f"{brain_url}/__stats")
                cut = counts(db)
                print(
                    f"kill {kill}: SIGKILL mid-send; BRAIN had {at_kill['sent']} sends; "
                    f"rows left PENDING: {cut['simulations'].get('PENDING', 0)}"
                )
                app = start_app(python, brain_url, args.port, env, home)
                wait_for(f"{app_url}/api/health")
                sims_at_restart = at_kill["simulations"]
                time.sleep(args.after * 60)
                stats = get(f"{brain_url}/__stats")
                print(
                    f"  {args.after:.0f} min after restart: "
                    f"{stats['simulations'] - sims_at_restart} more simulations finished"
                )
                if stats["simulations"] - sims_at_restart < 10:
                    failures.append(f"kill {kill}: the task did not carry on after the restart")
        finally:
            for proc in (app, brain):
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()

        final = counts(db)
        twice = stats["sent_twice"]
        with sqlite3.connect(db) as conn:
            orphaned = conn.execute(
                "SELECT count(*) FROM simulation_record WHERE status = 'ORPHANED'"
            ).fetchone()[0]
            adopted = conn.execute(
                "SELECT count(*) FROM simulation_record WHERE message LIKE 'Recovered after%'"
            ).fetchone()[0]
            requeued = conn.execute(
                "SELECT count(*) FROM simulation_record WHERE orphan_requeues > 0"
            ).fetchone()[0]
        print(f"BRAIN received {stats['sent']} sends; bodies sent twice: {len(twice)}")
        print(f"simulation rows by status: {final['simulations']}")
        print(f"trial rows by state: {final['trials']}")
        print(
            f"lost sends adopted: {adopted}; still ORPHANED: {orphaned}; queued again: {requeued}"
        )
        if not adopted and not orphaned:
            failures.append("the send the kill cut off was neither adopted nor waiting")
        if requeued:
            failures.append(f"{requeued} lost sends queued again although their Alphas existed")
        if twice:
            failures.append(f"{len(twice)} simulation bodies reached BRAIN twice")
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        raise SystemExit(1)
    print("PASS: no double sends, the task carried on after every restart")


if __name__ == "__main__":
    main()
