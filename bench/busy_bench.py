"""What the backend costs while a long Search Lab task keeps BRAIN's eight slots busy (Linux).

Starts ``fake_brain.py``, seeds a fresh data folder with a Search Lab task that already has
``--seed-trials`` behind it, starts the backend pointed at the stand-in, runs the task for
``--minutes`` and reads the backend's CPU from ``/proc`` each minute. Reports CPU as a share of
one core, and per thousand simulations, so builds that finish different amounts compare fairly.

The backend is pointed at the stand-in by a small launcher that sets ``API_BASE`` in memory
before the app is imported: the app itself has no setting that redirects it, on purpose.

    python3 bench/busy_bench.py --backend backend --minutes 20 --seed-trials 10000
"""

# ruff: noqa: T201 - a benchmark reports on stdout

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Run inside the backend's Python: point the app at the stand-in, then serve it as the
#: app's own entry point does where it can (Lean's QuietServer), else plain uvicorn.
LAUNCHER = """
import sys
import uvicorn
import alpha_harness.brain.platform as platform
platform.API_BASE = sys.argv[1]
from alpha_harness.main import app
config = uvicorn.Config(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
try:
    from alpha_harness.__main__ import QuietServer as Server
except ImportError:
    Server = uvicorn.Server
Server(config).run()
"""


def cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def get(url: str, method: str = "GET") -> dict:
    request = urllib.request.Request(  # noqa: S310 - loopback only
        url, method=method, headers={"Host": "localhost", "x-harness-client": "1"}
    )
    with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310
        body = answer.read()
    return json.loads(body) if body else {}


def maths(app_url: str) -> float | None:
    """CPU seconds the compute pool has spent (Lean's ``/api/resources``), if it says."""
    try:
        return float(get(f"{app_url}/api/resources")["computeCpuSeconds"])
    except OSError, KeyError, ValueError:
        return None


def wait_for(url: str) -> None:
    for _ in range(240):
        try:
            get(url)
            return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"{url} never answered")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="backend")
    parser.add_argument("--minutes", type=float, default=20.0)
    parser.add_argument("--sim-seconds", type=float, default=30.0)
    parser.add_argument("--seed-trials", type=int, default=10000)
    parser.add_argument("--fields", type=int, default=1500)
    parser.add_argument("--profile", default="balanced")
    parser.add_argument("--port", type=int, default=8797)
    parser.add_argument("--brain-port", type=int, default=8899)
    args = parser.parse_args()
    backend = Path(args.backend).resolve()
    python = str(backend / ".venv" / "bin" / "python")
    app_url = f"http://127.0.0.1:{args.port}"
    brain_url = f"http://127.0.0.1:{args.brain_port}"

    with tempfile.TemporaryDirectory() as home:
        env = {
            **os.environ,
            "HOME": home,
            "HARNESS_PROFILE": args.profile,
            "ALPHA_HARNESS_SHELL": "bench",
        }
        seed = [str(HERE / "busy_seed.py"), "--db", f"{home}/.alpha-harness/harness.db"]
        seed += ["--trials", str(args.seed_trials), "--fields", str(args.fields)]
        subprocess.run([python, *seed], env=env, cwd=home, check=True)  # noqa: S603
        brain = subprocess.Popen(  # noqa: S603
            [
                python,
                str(HERE / "fake_brain.py"),
                "--port",
                str(args.brain_port),
                "--sim-seconds",
                str(args.sim_seconds),
            ],
            cwd=home,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app = subprocess.Popen(  # noqa: S603
            [python, "-c", LAUNCHER, brain_url, str(args.port)],
            cwd=home,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for(f"{brain_url}/__stats")
            wait_for(f"{app_url}/api/health")
            get(f"{app_url}/api/lab-tasks/run-all", method="POST")
            start_cpu, start_at = cpu_seconds(app.pid), time.monotonic()
            last_cpu, last_sims = start_cpu, 0
            last_maths = maths(app_url)
            minutes: list[tuple[float, int]] = []
            print(
                f"{backend.parent.name}: {args.seed_trials} trials seeded, "
                f"{args.sim_seconds:.0f} s simulations, profile {args.profile}"
            )
            print(f"{'minute':>6} {'CPU':>9} {'maths':>9} {'sims':>6}")
            for minute in range(1, int(args.minutes) + 1):
                time.sleep(60)
                cpu, sims = cpu_seconds(app.pid), int(get(f"{brain_url}/__stats")["simulations"])
                now_maths = maths(app_url)
                share = (
                    f"{(now_maths - last_maths) / 60 * 100:>8.1f}%"
                    if now_maths is not None and last_maths is not None
                    else f"{'-':>9}"
                )
                minutes.append((cpu - last_cpu, sims - last_sims))
                print(
                    f"{minute:>6} {(cpu - last_cpu) / 60 * 100:>8.1f}% {share} "
                    f"{sims - last_sims:>6}",
                    flush=True,
                )
                last_maths = now_maths
                last_cpu, last_sims = cpu, sims
            stats = get(f"{brain_url}/__stats")
        finally:
            for proc in (app, brain):
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
    spent = last_cpu - start_cpu
    elapsed = time.monotonic() - start_at
    tail = minutes[-5:]
    tail_cpu = sum(c for c, _ in tail) / (60 * len(tail)) * 100 if tail else 0.0
    print(
        f"total: {spent:.1f} CPU-s over {elapsed / 60:.1f} min "
        f"({spent / elapsed * 100:.1f}% of a core; last 5 min {tail_cpu:.1f}%), "
        f"{last_sims} simulations, "
        f"{spent / max(last_sims, 1) * 1000:.0f} CPU-s per 1,000"
    )
    if stats.get("unknown"):
        print("not answered by the stand-in:", stats["unknown"])


if __name__ == "__main__":
    main()
