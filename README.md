# labs harness

## What this is

A harness for testing a goal-driven mobile agent. You give the agent a goal in
plain language ("save this contact's phone number into a note"); it carries it
out across Android apps on an emulator by reading screens and tapping, and it
stops for confirmation before anything irreversible. The harness seeds the
device, scores the outcome from independent evidence (app state, structured
data, vendor APIs; never the agent's own report) and records a replayable
trace. A web UI queues runs and shows the scoreboard; `deploy/REVIEWER.md`
explains how to reach the hosted UI. Interfaces are documented in
`harness/CONTRACTS.md`.

## Example flows

Each task belongs to a flow, and each flow answers one question about the agent
(`harness/tasks/catalog.py`, `python -m harness.cli tasks`).

| Flow | Question | Example task |
| --- | --- | --- |
| A Goal completion | Can it finish a single-app goal from plain language? | Create a Note (Markor) |
| B Information transfer | Can it carry a fact from one app into another intact? | Save Contact Details: Contacts → Markor note |
| C UI variation | Does it still succeed when labels and layout change? | Checkout on an Alternate UI (Jetsnack) |
| D Orchestration | Can it combine several apps into one outcome, within limits? | Order Recap Handoff: Jetsnack orders → note → text drafted but not sent |
| E Hybrid routing | Does it pick between a structured source and the screens, and notice stale data? | Order Total (Stale Data) |
| F Stop-and-confirm | Does it ask first, with an accurate summary, then do exactly what was approved? | Place an Order; Send a Text Message |
| H Infeasible goal | Does it say so honestly and change nothing? | Cancel an Order (Unsupported); Pay a Nonexistent Invoice |
| Drift | Can it report where a data source and the UI disagree? | Detect Data Mismatch |

The business tasks chain real apps on a dedicated emulator snapshot: tracked
hours from TimeCamp and the client's rate from Insightly CRM become a one-line
draft invoice in Invoice Ninja, checked through each vendor's API.

## Setup (once)

```sh
cd labs                               # the clone
python3 -m venv .venv                 # Python >= 3.12
.venv/bin/pip install -e .            # deps come from pyproject.toml only
```

Third-party APKs are not in git. Download Markor into `apks/` as described in
`apks/README.md`; the sample app is built per `sample-app/BUILD.md` into
`sample-app/dist/snackorders-debug.apk` (ignored).

## Emulator

The AVD name and port default to `p2_harness_api36` / `5584` and can be
overridden with `LABS_AVD_NAME` / `LABS_AVD_PORT` (set before any harness
import; the serial is `emulator-<port>`). `LABS_SYSTEM_IMAGE` overrides the
system image (`system-images;<api>;<tag>;<abi>`, default the arm64
`google_apis` image), e.g. an x86_64 image on a cloud VM.

```sh
.venv/bin/python -m harness.emulator.manager create                 # fresh AVD, target + 6G data fixed
.venv/bin/python -m harness.emulator.manager provision --windowed   # boot, Markor, loopback block, `baseline`
.venv/bin/python -m harness.emulator.manager restore                # back to `baseline`
.venv/bin/python -m harness.emulator.manager stop
```

## Running

`ANTHROPIC_API_KEY` must be set in the environment for real runs. Never commit
or print it.

```sh
.venv/bin/python -m harness.cli tasks
.venv/bin/python -m harness.cli run --task a_markor_note --confirm approve --windowed
.venv/bin/python -m harness.cli run --task a_markor_note --fake      # no emulator, no API
.venv/bin/python -m harness.cli run --goal "..." --allow-chrome           # freeform only; scored tasks never get Chrome
.venv/bin/python -m harness.cli replay <run_id>
.venv/bin/python -m harness.server --port 8765                        # web UI, localhost only
```

Runs are written under `runs/<run_id>/` (ignored).

## Tests

```sh
cd labs
PYTHONPATH=$PWD .venv/bin/python -m pytest -q
```

From a worktree, run the same command inside that worktree so `PYTHONPATH`
points at its code (the editable install points at the repo root).
