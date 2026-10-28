# Alpha Harness Lean

A fork of Alpha Harness `v2026.10.24.1` that keeps every lab, tool and screen but stops the
app's own CPU cost from growing with how long a task has run. BRAIN throughput is unchanged:
eight slots, batches of ten, the same submission and recovery rules.

Version: `2026.10.24.1+lean2`. The `+lean` label makes the updater report new upstream
releases without installing them by itself, since an upstream build would undo these changes.

## What changed

| Area | Before | Lean |
| --- | --- | --- |
| Search / Template Lab sampler | TPE replayed every trial on every ask; cost grew with the task | Replays the best 500 and latest 1,500 trials (profile-set); a study past 4,000 in memory is rebuilt from that window |
| Sampler time per refill | Up to `want × 5` asks, no limit | A CPU budget per refill (1 s Lean, 2 s Balanced, 5 s Max); the rest of the round is drawn uniformly from untried points as fixed trials the sampler still learns from |
| Evolution Lab | Read and decoded every child's score every 5 s, even with nothing to breed | Returns at once when there is nothing to breed or finish; the stall verdict is cached until another child is spent |
| Power Pool Lab | Loaded every proposed trial every 5 s | Counts them; loads only the ones it sends |
| `GET /api/lab-tasks` | Three scans with `json_extract` over every trial on every call | Recounts a task only when its counts by state changed |
| DuckDB | All cores, 25% of RAM | Threads and memory set by the profile |
| numpy / BLAS | All cores | Threads set by the profile, before numpy loads |
| Worker threads (`asyncio.to_thread`) | up to cores + 4 | 2 Lean, 4 Balanced |
| CPU priority | normal | nice 10 Lean, 5 Balanced |
| Keep-awake | blocked sleep and idle, on battery too | Lean and Balanced block sleep only, and only on mains power |
| Hidden window | Refetched on every push | Refetches once when shown again; desktop notifications poll every 30 s while hidden |
| Clocks (`useNow`) | Ticked every second forever | Paused while hidden |
| Sync shimmer | Animated `background-position` (a repaint every frame) | `transform` on a pseudo-element (composited) |
| Sidebar attention flash | Infinite | Three flashes |
| Portfolio during a sync | Recomputed on every progress step | At most every 15 s, and once when the sync ends |
| macOS window in the tray | `backgroundThrottling: disabled` | `throttle` |

## Phase 3: structure

| Area | Before | Lean |
| --- | --- | --- |
| Tracker | Woke every 1 s and queried the active rows, open or idle | Sleeps until the earliest Retry-After is due; with nothing to read, until a submission wakes it or the 60 s stale-send sweep |
| Batch engine | A round every 2 s, open or idle | Every 2 s while work runs, is queued or is being read back; otherwise asleep until work is queued, a simulation settles or the quota resets |
| Optimizer | Advanced every running task every 5 s | Advances when a simulation settles, an Alpha is stored or a task changes; 15 s safety tick (60 s with no task running) |
| Event bus | none | `events.py`: `submitted`, `queued`, `settled`, `task_changed`, `stored`; coalescing, never authoritative (every loop still reads its state from disk) |
| Active-set pushes | One per change | At most one a second, the last state always sent |
| uvicorn housekeeping | Woke 10 times a second | Once a second in the app's entry point (`QuietServer`) |
| Heavy arithmetic | Shared `asyncio.to_thread`'s pool at the process's priority | Its own threads (`compute.py`), as many as the profile's math threads, at `SCHED_IDLE` on Linux under Lean and Balanced; the event loop keeps its priority |
| Portfolio, vault overview, submittable | Every request started its own run | Requests with the same inputs share one run (`singleflight.py`); a run nobody waits for any more is cancelled at its next step |
| Search / Template refill memory | Reloaded and decoded every trial on each refill | Kept per task; only trials created or finished since are read, with a full reload every 25 refills (identical result, about a tenth of the time) |

Not done as first planned, and why:

- **A separate worker process** was replaced by low-priority compute threads. The sampler's
  studies live in this process; a second interpreter would hold a second copy of numpy,
  Optuna and DuckDB, and what is being bounded is CPU time, which a process boundary does not
  reduce. Linux schedules threads individually, so `SCHED_IDLE` threads give the same
  "runs only on spare CPU" behaviour.
- **A counters table** for the task list was not needed: Phase 1's cache recounts a task only
  when its counts by state change, which is exact and keeps repeat calls at about 40 ms.

## Resource profiles

Settings → Simulations → **Resource profile**. Threads and CPU priority take effect at the next
start; the rest at once. The choice is kept in `~/.alpha-harness/profile`; `HARNESS_PROFILE` in
the environment is the default when nothing was chosen there.

| | Lean | Balanced (default) | Max (as before) |
| --- | --- | --- | --- |
| numpy / BLAS threads | 1 | 2 | all |
| DuckDB threads | 1 | 2 | all |
| DuckDB memory | 15% of RAM | 25% | 25% |
| Worker threads | 2 | 4 | default |
| Nice | 10 | 5 | 0 |
| Sampler budget per refill | 1 s | 2 s | 5 s |
| Sampler history (best + latest) | 500 + 1,500 | 500 + 1,500 | 1,000 + 4,000 |
| Keep-awake | sleep only, on mains | sleep only, on mains | sleep and idle, always |
| UI motion | reduced | normal | normal |

## Measured

Idle, app open with nothing queued, started as people start it (`python -m alpha_harness`),
180 s window after 30 s to settle, from `/proc`:

| Build | CPU | Wake-ups |
| --- | --- | --- |
| Upstream v2026.10.24.1 | 0.68% of a core | 52.0 a second |
| Lean, Phases 0 to 2 | 0.62% | 50.6 a second |
| Lean, Phase 3, Balanced | 0.07% | 4.6 a second |
| Lean, Phase 3, Lean profile | 0.06% | 4.5 a second |

The upstream wake-ups were mostly uvicorn's ten-a-second housekeeping and the three loops'
fixed beats; what remains is the event loop's one-a-second housekeeping and SQLite's
connection threads.

From the benchmarks in `bench/`, run from `backend/`. CPU of one Search Lab refill of 10
points, 1,500-field pool:

| Trials in task | Shipped | Max | Balanced | Lean |
| --- | --- | --- | --- | --- |
| 2,000 | 1.07 s | 0.93 s | 0.74 s | 0.83 s |
| 10,000 | 4.77 s | 2.17 s | 0.96 s | 0.90 s |
| 30,000 | 15.95 s | 2.68 s | 1.02 s | 1.03 s |

A late task whose space is nearly all tried (60 fields, 10,000 trials): 8.36 s shipped,
1.01 s Lean.

`GET /api/lab-tasks` counting, 4 tasks of 100,000 trials: 633 ms CPU on a first call, 43 ms
on a repeat with no trial changed (what most pushes are).

```sh
python3 bench/idle_bench.py --backend backend --entry --seconds 180 --threads
cd backend
uv run python ../bench/sampler_bench.py --trials 2000 10000 30000
uv run python ../bench/sampler_bench.py --trials 10000 --fields 60 --narrow
uv run python ../bench/tasks_bench.py --tasks 4 --trials 100000
```

## Install on Arch Linux

```sh
sudo pacman -S uv nodejs pnpm
git clone <this repository> alpha-harness && cd alpha-harness && git switch lean
sh contrib/arch/install-lean.sh
```

This builds the UI, installs the backend with `uv tool`, and enables a systemd user service
(`contrib/systemd/alpha-harness.service`) with a kernel-enforced ceiling of 1.5 cores, idle
I/O priority and the Lean profile. Open <http://localhost:8000> in your browser. Run either
the service or the desktop app, never both: they share `~/.alpha-harness`.

## Still to do

- Desktop notifications sent by the backend, so a hidden window needs no queries at all.
- A fake-BRAIN server for an hour-long end-to-end CPU test with simulations running.
- Result caching for the Portfolio until the vault changes (single-flight shares runs that
  overlap, but a refresh after one ends still recomputes).
