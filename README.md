# labs harness

Goal-driven mobile agent harness: an Android emulator, a pinned Markor install,
seeded contacts/events/notes, and scored tasks run by a Claude agent. Design
notes live in `PLAN.md`, `NOTES.md` and `harness/CONTRACTS.md`.

## Setup (once)

```sh
cd /Users/rich/Desktop/labs
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
cd /Users/rich/Desktop/labs
PYTHONPATH=$PWD .venv/bin/python -m pytest -q
```

From a worktree, run the same command inside that worktree so `PYTHONPATH`
points at its code (the editable install points at the repo root).
