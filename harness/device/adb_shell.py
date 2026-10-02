"""Run adb against one device serial. Shared by the agent-facing and verifier sides."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from harness.contracts import DeviceError

SDK = Path(os.environ.get("ANDROID_HOME", Path.home() / "Library/Android/sdk"))
ADB = str(SDK / "platform-tools" / "adb")


def adb(serial: str, *args: str, timeout: float = 30.0, binary: bool = False) -> str | bytes:
    """Run `adb -s serial args...` and return stdout. Raise DeviceError on failure.

    Arguments are passed as a list (no host shell). Arguments after "shell" are
    still joined and interpreted by the device shell, so callers must quote them.
    """
    cmd = [ADB, "-s", serial, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeviceError(f"adb timed out after {timeout}s: {' '.join(args)}") from exc
    except OSError as exc:
        raise DeviceError(f"adb could not run: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).decode(errors="replace").strip()
        raise DeviceError(f"adb {' '.join(args)} failed ({proc.returncode}): {err}")
    return proc.stdout if binary else proc.stdout.decode(errors="replace")
