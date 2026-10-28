"""What the backend costs while it sits open with nothing to do (Linux only).

Starts a backend from the given ``backend/`` folder against an empty data folder, waits for
it to settle, then reads its CPU time and wake-ups (voluntary context switches, summed over
every thread) from ``/proc`` across a window. Run from anywhere:

    python bench/idle_bench.py --backend backend --seconds 120

Compare two checkouts by pointing ``--backend`` at each one's ``backend/`` (each needs its
own ``uv sync``).
"""

# ruff: noqa: T201 - a benchmark reports on stdout

import argparse
import os
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    return (int(fields[11]) + int(fields[12])) / ticks


def wakeups(pid: int) -> int:
    total = 0
    for task in Path(f"/proc/{pid}/task").iterdir():
        for line in (task / "status").read_text().splitlines():
            if line.startswith("voluntary_ctxt_switches"):
                total += int(line.split()[1])
    return total


def per_thread(pid: int) -> dict[str, int]:
    """Voluntary context switches by thread name."""
    found: dict[str, int] = {}
    for task in Path(f"/proc/{pid}/task").iterdir():
        name = (task / "comm").read_text().strip()
        for line in (task / "status").read_text().splitlines():
            if line.startswith("voluntary_ctxt_switches"):
                found[f"{name}#{task.name}"] = int(line.split()[1])
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="backend")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--warmup", type=float, default=30.0)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--profile", default="balanced")
    parser.add_argument("--threads", action="store_true", help="wake-ups by thread")
    parser.add_argument(
        "--entry",
        action="store_true",
        help="start the app as people do (python -m alpha_harness, port 8000) instead of uvicorn",
    )
    args = parser.parse_args()
    backend = Path(args.backend).resolve()
    python = backend / ".venv" / "bin" / "python"
    with tempfile.TemporaryDirectory() as home:
        # The shell variable keeps the entry point from opening a browser window.
        env = {
            **os.environ,
            "HOME": home,
            "HARNESS_PROFILE": args.profile,
            "ALPHA_HARNESS_SHELL": "bench",
        }
        port = 8000 if args.entry else args.port
        argv = (
            [str(python), "-m", "alpha_harness"]
            if args.entry
            else [str(python), "-m", "uvicorn", "alpha_harness.main:app", "--port", str(port)]
        )
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            argv,
            cwd=backend,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            url = f"http://127.0.0.1:{port}/api/health"
            request = urllib.request.Request(url, headers={"Host": "localhost"})
            for _ in range(120):
                try:
                    urllib.request.urlopen(request, timeout=1)  # noqa: S310
                    break
                except OSError:
                    time.sleep(0.5)
            time.sleep(args.warmup)
            cpu0, wake0, by0 = cpu_seconds(proc.pid), wakeups(proc.pid), per_thread(proc.pid)
            time.sleep(args.seconds)
            cpu1, wake1, by1 = cpu_seconds(proc.pid), wakeups(proc.pid), per_thread(proc.pid)
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=30)
    used = cpu1 - cpu0
    print(
        f"{backend.parent.name}: {used:.2f} CPU-s over {args.seconds:.0f} s idle "
        f"({used / args.seconds * 100:.2f}% of a core), "
        f"{(wake1 - wake0) / args.seconds:.1f} wake-ups/s"
    )
    if args.threads:
        rates = {k: (v - by0.get(k, 0)) / args.seconds for k, v in by1.items()}
        for name, rate in sorted(rates.items(), key=lambda kv: -kv[1])[:10]:
            if rate > 0:
                print(f"  {rate:7.2f}/s  {name}")


if __name__ == "__main__":
    main()
