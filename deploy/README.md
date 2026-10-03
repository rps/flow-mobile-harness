# deploy/ — running the harness on the GCE VM

Phase 2 Area F. Scripts and runbook to host the harness on `<VM>` (x86_64, KVM,
Ubuntu 22.04) behind IAP for the owner and a tunnel-only ssh account for outside reviewers
(`REVIEWER.md`). Real runs by default with confirmations auto-approved; the VM holds the API key
(see "API key"). Nothing here touches a Mac emulator.

## What ships

| File | Runs on | Purpose |
| --- | --- | --- |
| `lib/gcloud.sh` | Mac | IAP ssh/scp helpers with retry on ssh exit 255 only; start/stop/status |
| `push_source.sh [REF]` | Mac | `git bundle create` of a branch/tag/commit, scp over IAP, checkout into `~/labs` as branch `deploy`; preserves the VM's provisioned baseline.json/stock_apps.txt. Never rsyncs; .env/.venv/apks/runs cannot leak. |
| `bootstrap_vm.sh` | VM | Idempotent: Python 3.12 (deadsnakes PPA), Android cmdline-tools/platform-tools/emulator + `system-images;android-36.1;google_apis;x86_64` under `~/android-sdk`, `.venv` from pyproject, pinned Markor APK with SHA-256 check, `~/labs-env.sh` |
| `serve.sh start|stop|status|token|run|install-service` | VM | `harness.server` on 127.0.0.1:8765, `--policy-default approve`, stable token in `~/.config/labs/ui_token`; sources `~/labs-env.sh` and `~/.config/labs/env`, requires ANDROID_HOME + LABS_AVD_NAME/PORT/SYSTEM_IMAGE; `install-service` makes it a boot-time systemd user service |
| `reviewer/setup_reviewer.sh` | VM | key-only `reviewer` user that sshd allows to forward 127.0.0.1:8765 and nothing else |
| `reviewer/make_handoff.sh` | Mac | key, VM account, UI service, autostop off, filled `private/REVIEWER.md` (gitignored) |
| `REVIEWER.md` | | template of the reviewer's guide: plain `ssh -N -L` tunnel, no gcloud or Google account |
| `check_emulator.sh` | VM | Throwaway AVD `cloud_check` on 5586: headless boot, adb root, iptables REJECT of 10.0.2.2, loopback probe, snapshot save/load timing; deletes the AVD |
| `autostop/` | VM | systemd timer running `shutdown -h now` after IDLE_MINUTES without ssh connections or runs-dir writes; `install.sh` |
| `RUNBOOK.md` | | start, push, bootstrap, test, serve, tunnel, harness baseline AVD (§6b), emulator check, auto-stop, stop, real runs with the key (§10), business profile on the VM (§11) |
| `VM_STATE.md` | | inventory, reuse/replace decisions, measured results, timeline |
| AVD `cloud_business` (VM only, not a file) | VM | Play image `android-36.1;google_apis_playstore;x86_64`, port 5586, snapshot `business`: test Google account, TimeCamp + Insightly from Play, Invoice Ninja F-Droid APK, Markor; record in `~/.config/labs/business.json`. Selected with `LABS_BUSINESS_*` (RUNBOOK §11) |

Decisions: Python 3.12 via deadsnakes rather than uv (no new tool on the VM, apt-managed, pyproject
untouched). SDK reused from the owner's smoke-test install rather than replaced (same packages the
harness needs, 5.4 GB already on disk). The VM has no service account, so auto-stop uses
`shutdown -h now` instead of `gcloud compute instances stop`.

## Status after the Phase 2 merges (main e63e7dc on the VM)

- **B's `LABS_SYSTEM_IMAGE` / `LABS_AVD_NAME` / `LABS_AVD_PORT` overrides are merged.** `check_emulator.sh` reports `mode=manager`, and `manager provision` created the VM's kept harness baseline AVD `cloud_harness` on port 5584 (x86_64, Markor installed, loopback blocked, restore about 3 s; RUNBOOK §6b, VM_STATE.md round 2). `~/labs-env.sh` exports that AVD's name and port as defaults and `serve.sh` refuses to start without them.
- **B's restore retry, batched seeding and redaction** are merged. One real run on the VM completed with baseline restore and seeding inside a 224 s CLI total (182 s of it agent time); seeding was not timed separately and restore-retry was not triggered, so both remain **unverified** as individual numbers.
- **C's cancel** is merged. At ccaacd4 three cancel tests in tests/test_server_phase2.py failed on both the Mac and the VM (3 failed, 287 passed); fixed on main at e63e7dc. Two emulators at once used about 360% of 400% CPU, so two concurrent real runs on n2-standard-4 are likely CPU-bound (**unverified** for real runs).
- **A, sample-app provisioning**: `provision_sample_app.py` needs an x86_64-capable build of the sample app (BUILD.md builds for the Mac's arm64 AVD; a debug APK is multi-ABI only if its native deps are, **unverified**). Not run on the VM.
- **D, x86 business apps** (ABI results from area D, apps-probe/PROBE.md on D's branch, not re-checked here): TimeCamp (com.timecamp.mobile) and Insightly (com.insightly.droid) ship universal base APKs with x86_64 libs, so they install on the VM image as-is. Invoice Ninja (com.invoiceninja.app) from Play is split (base + config.arm64_v8a + locale/density); Play should serve a config.x86_64 split on an x86 device, **unverified**; the F-Droid APK in apps-probe/apks is universal with x86_64 and works. Play Store itself needs the `google_apis_playstore;x86_64` image (listed by sdkmanager, not installed here) and a Google account sign-in on the VM, which is an owner decision. **Update 2026-10-03:** done. The image is installed and the AVD `cloud_business` holds the test Google account, TimeCamp and Insightly from Play (x86_64 libs selected), Invoice Ninja from the F-Droid APK, and Markor (RUNBOOK §11, VM_STATE.md round 5). Google showed no device-verification prompt on the cloud machine.

## Multiple testers

Today: one shared UI token, one queue (the server runs jobs serially), one tunnel per tester
(`-L 8765:127.0.0.1:8765`). Owners tunnel through IAP; outside reviewers use the `reviewer` ssh
key (RUNBOOK §6a), which needs tcp:22 open to the internet (key-only) and a static IP. There is no
per-user identity in the UI. Rejected alternatives: an IAP HTTPS load balancer (~$18/month
**unverified**, and testers would need Google identities) and Caddy with basic auth on a public 443
(a web port to the world rather than sshd). During a review window the VM stays RUNNING with the
auto-stop disabled, since a reviewer cannot start it.

The emulator is also single-tenant: one AVD, one baseline snapshot, one run at a time. A second
tester's run waits in the queue.

## API key

Since 2026-10-03 the Mac's key is on the VM in `~/.config/labs/env` (0600) with HARNESS_BUDGET_USD=60 (was 10; owner decision 2026-10-03) and the two Invoice Ninja API variables,
by owner decision. RUNBOOK.md §10 has the run command, the refresh and the removal steps. The first real
scored run on the VM (a_markor_note) passed with the same 16 steps and ~$0.38 as the Mac runs; its agent
wall time was 182 s against 102-142 s for the nine Mac runs, a single sample (VM_STATE.md round 3).

## Costs

Compute + external IP bill while RUNNING (owner's estimate ~$0.20/h); disk ~$10/month while stopped.
Budget alert at $100 is alert-only. Always stop the VM or rely on the auto-stop.
