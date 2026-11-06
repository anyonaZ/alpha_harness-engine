# Alpha Harness Lean

A fork of Alpha Harness `v2026.10.24.1` that keeps every lab, tool and screen but stops the
app's own CPU cost from growing with how long a task has run. BRAIN throughput is unchanged:
eight slots, batches of ten, the same submission and recovery rules.

Upstream base: v2026.10.24.1 (the upstream release this fork is built on; update this line
when one is brought in, since the weekly upstream check compares from it).

Version: `2026.10.24.1+lean7`. The `+lean` label makes the updater report new upstream
releases without installing them by itself, since an upstream build would undo these changes.

## What changed

| Area | Before | Lean |
| --- | --- | --- |
| Search / Template Lab sampler | TPE replayed every trial on every ask; cost grew with the task | Replays the best 500 and latest 1,500 trials (profile-set); a study past 2,500 in memory (Max: 6,000) is rebuilt from that window |
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

## Phase 4: busy load, notices, and a window into usage

| Area | Before | Lean |
| --- | --- | --- |
| TPE group decomposition | Every finished trial merged into the parameter groups again on every ask | Each trial merged once (`labs/tpe_speed.py`); merging a set already merged changes nothing, so the groups are the same |
| TPE category positions | `tuple.index` over the choice list for every trial on every ask (1,500 fields deep) | A lookup table per list, first match first, as `tuple.index` gives |
| Sampler total | Bounded per refill only, so fast-returning batches could add up | Also a shared allowance: at most 20% (Lean) or 40% (Balanced) of a core on average, half a minute of burst; a round it cuts short is drawn uniformly and never mistaken for a used-up search |
| Notifications | The desktop window fetched the task list and engine status every 30 s while hidden, to compare them itself | The backend compares on the changes it already pushes and sends only the news (`notices.py`, the `notices` topic); a hidden window fetches nothing |
| Notifications in a browser | none | Settings → Tasks → Notify me in this browser, for the systemd service mode |
| Portfolio | Recomputed on every refresh | Kept until the vault is next written to (`Catalog.version`), shared between concurrent requests |
| Resources panel | none | Settings → Resources: the profile, CPU in the last 5 seconds and since start (and the sampler's part of it), threads, priority, keep-awake |

`bench/tpe_equivalence.py` replays one history into Optuna's sampler and into the app's, asks
both for the same points and fails on the first difference: identical over 2,000 and 4,000
trials with 1,500 fields and 1,500 trials with 60, at 21% to 32% less CPU per ask.

## Phase 5: flat under busy load

Phase 4's busy benchmark showed CPU still rising over a long task: the in-memory study grew
from its 2,000-trial window towards the 4,000-trial rebuild ceiling, and each ask costs more
with every trial held.

| Area | Before | Lean |
| --- | --- | --- |
| TPE trial split | With `group=True`, each ask split the same trials into good and bad once per parameter group (six times on a Search Lab task) | Remembered for the groups of one ask (`labs/tpe_speed.py`); the same trials and count give the same split |
| Rebuild ceiling | 4,000 trials (Max 10,000) | 2,500 (Max 6,000): window + 500. An ask costs about 12 µs per trial held and a rebuild about 1.5 s, so this margin costs least in all |
| Tracker poll round | Loaded every pending or running row whole, request bodies included, several times a second | Reads the five columns it needs; a row is loaded whole when it finishes |
| Live push of running simulations | Read and serialised every active row once a second whether or not a screen was open | Skipped with no screen open; a screen that connects gets a fresh snapshot (`Hub.provide`) |

`bench/tpe_equivalence.py` is still identical (3,000 trials, 120 asks), now 52% less CPU per
ask than Optuna as shipped. One ask: 32 ms at 2,000 trials held, 57 ms at 4,000 (Phase 4: 37
and 70).

### Busy load

`bench/busy_bench.py`, with a Search Lab task seeded with 10,000 trials and 1,500 fields, eight
slots busy, against the BRAIN stand-in (`bench/fake_brain.py`), Balanced. CPU is a share of
one core; "sampler" is the compute pool's part of it.

60 s simulations (about BRAIN's pace; 80 a minute with eight slots of ten):

| Build | Minutes | CPU | Last 5 min | Sampler | Simulations | CPU-s per 1,000 |
| --- | --- | --- | --- | --- | --- | --- |
| Upstream v2026.10.24.1 | 20 | 33.9%, in bursts of 50–57% | 33.0% | – | 960 (slots empty one minute in three) | 424 |
| Lean, Phase 4 | 20 | 16.1%, rising 8% → 20% | 19.5% | 12% at the end | 1,520 | 127 |
| Lean, Phase 5 | 30 | 12.0%, flat from minute 6 | 13.4% | 5–7% | 2,320 | 93 |
| Lean, Phase 5, window open | 30 | 13.1%, flat from minute 6 | 14.2% | 5–7% | 2,320 | 102 |

20 s simulations (three times BRAIN's pace, a stress test):

| Build | Minutes | CPU | Last 5 min | Simulations | CPU-s per 1,000 |
| --- | --- | --- | --- | --- | --- |
| Upstream v2026.10.24.1 | 10 | 64.6% | 70.8% | 960 | 404 |
| Lean, Phase 4 | 10 | 41.5%, rising 22% → 54% | 48.7% | 2,160 | 116 |
| Lean, Phase 5, window open | 10 | 31.1%, flat from minute 3 | 33.5% | 2,200 | 85 |

The plan's target was under 10% of a core at a steady 80 simulations a minute. Phase 5
holds 12–13%. What remains is about half sampler (one TPE ask per simulation, at roughly
35 ms each) and half the work itself: about three status reads a second, a whole Alpha and
its PnL and turnover downloaded for each simulation, and the progress writes the screens
show. The flat line is the point: it no longer grows with the task's age. At a slower
pace the sampler's half shrinks with it, since it asks once per simulation finished.

```sh
backend/.venv/bin/python bench/busy_bench.py --backend backend --minutes 30 --sim-seconds 60
backend/.venv/bin/python bench/busy_bench.py --backend backend --minutes 30 --sim-seconds 60 --watch
```

## Phase 6: the plan's acceptance gates

The plan set five gates besides CPU: the sampler's search quality, crash recovery, an hour
under load, ask cost at the extremes, and benchmarks that keep running. All are now measured,
and the benchmarks behind them are in `bench/`.

### Search quality (`bench/quality_replay.py`)

One synthetic Search Lab task, run twice per seed with the same sampler and scoring, differing
only in what the sampler learns from. Asked in batches of ten with the constant liar, as a
slot's batch is. Scored by a hidden objective TPE can learn (some fields, operators, lookbacks
and neutralizations are better; some fields pay only with one family; noise; 8% failures).
**Hits** are points in the top 1% of what uniform sampling finds. Pass: 95% of
all-history's hits and top-25 mean.

| Scenario, seeds | Learns from | Hits per 1,000 | Top-25 mean | Last-third hits | Sampler CPU |
| --- | --- | --- | --- | --- | --- |
| New task: 6,000 trials, 8 seeds | every trial (upstream) | 58.2 | 1.393 | 44.9 | 227 s |
| | **best 500 + latest 1,500** | **57.6 (98.9%)** | **1.382 (99.2%)** | 40.3 | 142 s |
| | + 1,000 evenly sampled | 53.9 (92.6%) | 1.378 (98.9%) | 35.4 | 175 s |
| Long task: 10,000 trials, then 3,000, 8 seeds | every trial (upstream) | 68.0 | 1.364 | 25.0 | 476 s |
| | **best 500 + latest 1,500** | **65.5 (96.3%)** | **1.300 (95.3%)** | 43.5 | 93 s |
| | + 1,000 evenly sampled | 75.0 (110%) | 1.345 (98.6%) | 43.6 | 131 s |

**The window passes both gates**, and is what every profile uses. The long task is the
narrow one: on three seeds the window failed it (73% of hits), carried by one seed where
learning from everything found a good region early, and eight seeds put it at 95.3% of the
top-25 mean, just over the line.

The window drops the record of what was tried long ago and found poor, which TPE's "bad"
model learns from. So I tried keeping an even sample of 1,000 older trials as well
(`sampler_spread`, rebuilt past 3,500). It did better on the long task (110% of hits, 98.6%
of the top-25 mean) and worse on the new one (92.6% of hits, under the gate), and cost about
two points of a core under busy load (13.9% against 13.1%). The long task starts from 10,000
uniformly random trials, which flatters old history: a real task's old trials were chosen by
TPE. So it stays a setting, off in every profile. Per-seed differences are large (a paired
standard deviation of 9 to 30 hits per 1,000), so a few percent either way is noise; a task
that has run for weeks is where to look if Search Lab ever seems to stall.

### Crash recovery (`bench/recovery_check.py`)

`fake_brain.py` now finishes simulations on its own and creates their Alphas, as BRAIN does,
counts every body sent, and can hold a send open (`--post-delay`). The check kills the backend
with SIGKILL while BRAIN holds a ten-simulation batch whose id the backend never received,
starts it again on the same data, and runs seven more minutes.

| Build | Lost sends adopted | Queued again | Sent twice | Simulations in the 7 min after |
| --- | --- | --- | --- | --- |
| Upstream v2026.10.24.1 | 10 of 10 | 0 | 0 | 720 |
| Lean | 10 of 10 | 0 | 0 | 940 |

Recovery behaves as upstream's: the stale-send sweep marks the rows orphaned, the orphan pass
finds their Alphas in the account's listing and adopts them, nothing is sent twice.

### An hour on a 20,000-trial task

`busy_bench.py --minutes 60 --seed-trials 20000 --sim-seconds 60 --watch`: 13.0% of a core over the hour, 101 CPU-s per 1,000 simulations, 4,639 simulations.
Flat: 11.6%, 12.8%, 14.2%, 13.4%, 12.6% and 13.6% for each ten minutes.

### Ask cost at the extremes

CPU per ask of a refill, `sampler_bench.refill`, freshly built and at the rebuild ceiling
(the most a study holds before it is rebuilt). Gate: under 150 ms everywhere.

| Trials, fields | Lean | Balanced | Max |
| --- | --- | --- | --- |
| 50,000, 1,500 | 36 ms | 28 ms | 66 ms |
| 10,000, 5,000 | 60 ms | 53 ms | 124 ms |
| 50,000, 5,000 | 63 ms | 66 ms | 125 ms |
| At the ceiling, 1,500 fields | 35 ms (2,500 held) | 35 ms (2,500 held) | 75 ms (6,000 held) |
| At the ceiling, 5,000 fields | 69 ms | 69 ms | 146 ms |
| With the even sample on (3,500 held) | | 61 ms; 98 ms at 5,000 fields | |

### Kept running

`lefthook run check`, and so CI, now lints `bench/` and runs `tpe_equivalence.py` on 1,000
trials: it fails if the app's sampler asks anything Optuna's would not, or if the shortcuts
stop saving at least 15% (they save about 45%). The longer benchmarks need minutes and a
stand-in server, so they stay manual; their commands are above.

### CPU by the hour

Settings → Resources has a **By hour** row: one bar per clock hour over the last day, and this
hour so far with the sampler's part (`usage.py`, read every five minutes; `hours` in
`GET /api/resources`). The plan asked for this so the budget can be checked without
watching the panel.

## Phase 7: the plan's last UI items

| Item | Before | Lean |
| --- | --- | --- |
| Closing the window | Hid it to the tray without a word: on a desktop that shows no tray icon, the app looked closed while tasks kept simulating | The first close says the app is still running, with **Keep running** and **Quit** (`desktop/src-tauri/src/main.rs`, once per install) |
| Loading placeholders | Pulsed for as long as a load took | Five slow pulses, ten seconds, then still (`animate-skeleton`); a progress bar of unknown length and "Reconnecting…" still pulse, as they mean work is going on |
| Portfolio on the Lean profile | Large selections were slow with no reason given | When a figure takes over three seconds on Lean, a note says why and offers **Use Balanced** (from the next start, when thread counts are set) |

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
| Sampler share of a core, on average | 20% | 40% | no limit |
| Sampler history (best + latest) | 500 + 1,500 | 500 + 1,500 | 1,000 + 4,000 |
| Study rebuilt past | 2,500 trials | 2,500 trials | 6,000 trials |
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
git clone https://github.com/anyonaZ/alpha_harness-engine && cd alpha_harness-engine
sh contrib/arch/install-lean.sh
```

This builds the UI, installs the backend with `uv tool`, and enables a systemd user service
(`contrib/systemd/alpha-harness.service`) with a kernel-enforced ceiling of 1.5 cores, idle
I/O priority and the Lean profile. Open <http://localhost:8000> in your browser. Run either
the service or the desktop app, never both: they share `~/.alpha-harness`.

In browser mode, turn on **Settings → Tasks → Notify me in this browser** to get the desktop
app's notifications (a task ending, BRAIN's daily limit, a refused session) while the tab is
in the background.

## Still to do

- Upstream pull requests to residual-lab/alpha-harness, one per change, if its maintainers
  want them.
