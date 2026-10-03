# deploy/ — running the harness on the GCE VM

Phase 2 Area F. Scripts and runbook to host the harness on `<VM>` (x86_64, KVM,
Ubuntu 22.04) behind IAP. Fake mode only in this phase; nothing here touches a Mac emulator.

## What ships

| File | Runs on | Purpose |
| --- | --- | --- |
| `lib/gcloud.sh` | Mac | IAP ssh/scp helpers with retry on ssh exit 255 only; start/stop/status |
| `push_source.sh [REF]` | Mac | `git bundle create` of a branch/tag, scp over IAP, checkout into `~/labs` as branch `deploy`. Never rsyncs; .env/.venv/apks/runs cannot leak. |
| `bootstrap_vm.sh` | VM | Idempotent: Python 3.12 (deadsnakes PPA), Android cmdline-tools/platform-tools/emulator + `system-images;android-36.1;google_apis;x86_64` under `~/android-sdk`, `.venv` from pyproject, pinned Markor APK with SHA-256 check, `~/labs-env.sh` |
| `serve.sh start|stop|status|token` | VM | `harness.server` on 127.0.0.1:8765, `--fake-default`, stable token in `~/.config/labs/ui_token` |
| `check_emulator.sh` | VM | Throwaway AVD `cloud_check` on 5584: headless boot, adb root, iptables REJECT of 10.0.2.2, loopback probe, snapshot save/load timing; deletes the AVD |
| `autostop/` | VM | systemd timer running `shutdown -h now` after IDLE_MINUTES without ssh connections or runs-dir writes; `install.sh` |
| `RUNBOOK.md` | | start, push, bootstrap, test, serve, tunnel, emulator check, auto-stop, stop, API-key step |
| `VM_STATE.md` | | inventory, reuse/replace decisions, measured results, timeline |

Decisions: Python 3.12 via deadsnakes rather than uv (no new tool on the VM, apt-managed, pyproject
untouched). SDK reused from the owner's smoke-test install rather than replaced (same packages the
harness needs, 5.4 GB already on disk). The VM has no service account, so auto-stop uses
`shutdown -h now` instead of `gcloud compute instances stop`.

## Blocked on other areas (not duplicated here)

- **B, `LABS_SYSTEM_IMAGE` override in manager.py** (plus `LABS_AVD_NAME`/`LABS_AVD_PORT`, commit cb112c2 on feature/harness-hardening): until merged, `check_emulator.sh` runs in `mode=manual` (creates/boots the AVD itself) and `manager provision` cannot target the x86_64 image. `_image_api_level()` also hard-codes the arm64 path and needs B's change.
- **B, restore retry and batched seeding; redaction**: real runs on the VM inherit whatever lands on main; nothing in deploy/ depends on them, but the headless VM is where Chrome/Vulkan restore failures were most likely, so re-measure after B merges.
- **C, cancel**: the serve/tunnel path is unaffected; the UI gains cancel when C merges.
- **A, sample-app provisioning**: `provision_sample_app.py` needs an x86_64 build of the sample app (BUILD.md builds for the Mac's arm64 AVD; a debug APK is multi-ABI only if its native deps are, **unverified**).
- **D, x86 business apps** (ABI results from area D, apps-probe/PROBE.md on D's branch, not re-checked here): TimeCamp (com.timecamp.mobile) and Insightly (com.insightly.droid) ship universal base APKs with x86_64 libs, so they install on the VM image as-is. Invoice Ninja (com.invoiceninja.app) from Play is split (base + config.arm64_v8a + locale/density); Play should serve a config.x86_64 split on an x86 device, **unverified**; the F-Droid APK in apps-probe/apks is universal with x86_64 and works. Play Store itself needs the `google_apis_playstore;x86_64` image (listed by sdkmanager, not installed here) and a Google account sign-in on the VM, which is an owner decision.

## Multiple testers

Today: one shared UI token (printed by `serve.sh token`), one queue (the server runs jobs serially),
one tunnel per tester (`-L 8765:127.0.0.1:8765`). Anyone with project IAP + OS Login access can open a
tunnel; there is no per-user identity in the UI. Two options need owner approval because each costs
money and adds a public surface:

- IAP-protected HTTPS load balancer in front of 8765: no tunnels, Google-identity login, ~$18/month
  for the forwarding rule plus traffic (**unverified** estimate, check the pricing page).
- Caddy on the VM with a public 443 and basic auth or OAuth: cheapest, but opens a port to the world,
  which the owner has ruled out so far.

The emulator is also single-tenant: one AVD, one baseline snapshot, one run at a time. A second
tester's run waits in the queue.

## API key

No key is on the VM. Real runs are an owner decision; RUNBOOK.md step 10 describes a dedicated key
with its own spend limit kept in `~/.config/labs/env` (0600), exported into the server's environment
only, and revoked after the window.

## Costs

Compute + external IP bill while RUNNING (owner's estimate ~$0.20/h); disk ~$10/month while stopped.
Budget alert at $100 is alert-only. Always stop the VM or rely on the auto-stop.
