"""Waiting for the UI to settle after an action, before the next observation."""

import pytest

from harness.agent.loop import run_agent
from harness.contracts import Config, ConfirmationDecision, DeviceError, TerminationReason
from harness.device import adb as adb_mod
from harness.device import adb_shell as adb_shell_mod
from harness.device.adb import AdbDevice, SettleResult, tree_packages, wait_for_settle, window_package
from tests.agent_fakes import FakeDevice, ScriptedModel, tool

APP = "Window{594f243 u0 com.labs.snackorders/com.example.jetsnack.ui.MainActivity}"
LAUNCHER = "Window{1a2b u0 com.google.android.apps.nexuslauncher/com.google.android.apps.nexuslauncher.NexusLauncherActivity}"
LAUNCHER_TREE = 'screen 576x1280 (screenshot coordinates); packages: com.google.android.apps.nexuslauncher\nTextView "Jetsnack"'
APP_LOADING = "screen 576x1280 (screenshot coordinates); packages: com.labs.snackorders\nView id=spinner"
APP_TREE = 'screen 576x1280 (screenshot coordinates); packages: com.labs.snackorders\nTextView "Android\'s picks"'


class FakeClock:
    """Time advances only when the code under test sleeps or a read 'takes' time."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        assert s >= 0
        self.t += s


class Script:
    """Focus and dump readers that return scripted values, repeating the last,
    and charge the fake clock per read (a dump costs more, as on the emulator)."""

    def __init__(self, clock, focuses, trees, focus_cost=0.1, dump_cost=1.0):
        self.clock, self.focuses, self.trees = clock, list(focuses), list(trees)
        self.focus_cost, self.dump_cost = focus_cost, dump_cost
        self.focus_reads = self.dumps = 0

    def focus(self):
        self.focus_reads += 1
        self.clock.t += self.focus_cost
        return self.focuses.pop(0) if len(self.focuses) > 1 else self.focuses[0]

    def dump(self):
        self.dumps += 1
        self.clock.t += self.dump_cost
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]


def _settle(script, clock, max_s=6.0, before=None):
    return wait_for_settle(script.focus, script.dump, max_s=max_s, clock=clock, sleep=clock.sleep, before=before)


# --- wait_for_settle --------------------------------------------------------


def test_dump_that_changes_once_then_stabilises_settles_on_the_final_tree():
    clock = FakeClock()
    s = Script(clock, [APP], [APP_LOADING, APP_TREE, APP_TREE])
    result = _settle(s, clock)
    assert result.reason == "stable"
    assert result.tree == APP_TREE
    assert s.dumps == 3  # loading, final, final again
    assert result.seconds == pytest.approx(clock.t - 100.0)
    assert result.seconds < 6.0


def test_dump_that_never_stabilises_hits_the_cap_and_returns_no_tree():
    clock = FakeClock()
    trees = [f"screen 1x1 (screenshot coordinates); packages: com.labs.snackorders\nframe {i}" for i in range(50)]
    s = Script(clock, [APP], trees)
    result = _settle(s, clock, max_s=6.0)
    assert result.reason == "cap"
    assert result.tree is None
    assert result.seconds >= 6.0
    # no new dump starts once the cap has passed; one already in flight may overrun it
    assert result.seconds < 6.0 + s.dump_cost + s.focus_cost + 1e-9


def test_cold_start_does_not_settle_on_identical_launcher_dumps():
    """The round-2 launch race: after tapping the icon, focus is null for
    seconds and the dump keeps showing the launcher even once the app window
    has focus. Two identical launcher dumps must not count as settled."""
    clock = FakeClock()
    focuses = [None] * 20 + [APP]
    trees = [LAUNCHER_TREE, LAUNCHER_TREE, APP_TREE]
    s = Script(clock, focuses, trees)
    result = _settle(s, clock, max_s=10.0)
    assert result.reason == "stable"
    assert result.tree == APP_TREE
    assert s.dumps == 4  # two stale launcher dumps rejected, then the app twice


def test_focus_that_stays_null_hits_the_cap_without_dumping():
    clock = FakeClock()
    s = Script(clock, [None], [APP_TREE])
    result = _settle(s, clock, max_s=2.0)
    assert result == SettleResult(result.seconds, "cap", None)
    assert result.seconds >= 2.0
    assert s.dumps == 0


def test_focus_change_between_dumps_restarts_the_comparison():
    clock = FakeClock()
    other = "Window{77 u0 com.google.android.permissioncontroller/com.android.permissioncontroller.GrantPermissionsActivity}"
    perm_tree = "screen 1x1 (screenshot coordinates); packages: com.google.android.permissioncontroller\nButton \"Allow\""
    # stable APP focus, one APP dump, then a dialog takes focus before the second dump
    s = Script(clock, [APP, APP, APP, other], [APP_TREE, perm_tree])
    result = _settle(s, clock)
    assert result.reason == "stable"
    assert result.tree == perm_tree  # never settled on APP_TREE despite it appearing once


def test_window_without_a_package_skips_the_package_check():
    clock = FakeClock()
    shade = "Window{9 u0 NotificationShade}"
    shade_tree = "screen 1x1 (screenshot coordinates); packages: com.android.systemui\nText \"Wi-Fi\""
    s = Script(clock, [shade], [shade_tree])
    result = _settle(s, clock)
    assert (result.reason, result.tree) == ("stable", shade_tree)


def test_zero_cap_returns_immediately():
    clock = FakeClock()
    s = Script(clock, [APP], [APP_TREE])
    result = _settle(s, clock, max_s=0.0)
    assert result.reason == "cap"
    assert s.dumps == 0


def test_unchanged_window_settles_after_the_focus_gate_without_a_dump():
    clock = FakeClock()
    s = Script(clock, [APP], [APP_TREE])
    result = _settle(s, clock, before=APP)
    assert (result.reason, result.tree, result.focus) == ("focus_only", None, APP)
    assert s.dumps == 0
    assert s.focus_reads == 2


def test_window_change_from_before_requires_two_identical_dumps():
    clock = FakeClock()
    s = Script(clock, [APP], [APP_LOADING, APP_TREE, APP_TREE])
    result = _settle(s, clock, before=LAUNCHER)
    assert (result.reason, result.tree) == ("stable", APP_TREE)
    assert s.dumps == 3


def test_transition_back_to_the_same_window_still_counts_as_changed():
    """A null read means a transition happened, even if focus ends where it began."""
    clock = FakeClock()
    s = Script(clock, [None, None, APP], [APP_LOADING, APP_TREE, APP_TREE])
    result = _settle(s, clock, before=APP)
    assert (result.reason, result.tree) == ("stable", APP_TREE)


def test_dump_listing_no_packages_settles_on_equality_instead_of_capping():
    """Image-only, canvas or WebView screens trim to a header with no packages."""
    clock = FakeClock()
    empty = "screen 576x1280 (screenshot coordinates); packages: -"
    s = Script(clock, [APP], [empty])
    result = _settle(s, clock, before=LAUNCHER)
    assert (result.reason, result.tree) == ("stable", empty)
    assert s.dumps == 2


def test_dump_listing_other_packages_is_still_rejected():
    clock = FakeClock()
    s = Script(clock, [APP], [LAUNCHER_TREE])
    result = _settle(s, clock, max_s=6.0, before=LAUNCHER)
    assert (result.reason, result.tree) == ("cap", None)


@pytest.mark.parametrize("focus, package", [
    (APP, "com.labs.snackorders"),
    (LAUNCHER, "com.google.android.apps.nexuslauncher"),
    ("Window{5 u0 com.labs.snackorders}", "com.labs.snackorders"),
    ("Window{9 u0 NotificationShade}", None),
    ("Window{3 u0 PopupWindow:4ab1c}", None),
])
def test_window_package(focus, package):
    assert window_package(focus) == package


def test_tree_packages_reads_the_header_only():
    assert tree_packages("screen 1x1 (screenshot coordinates); packages: a.b, c.d\nText \"packages: x.y\"") == ["a.b", "c.d"]
    assert tree_packages("screen 1x1 (screenshot coordinates); packages: -") == []


# --- AdbDevice.settle over a fake adb ---------------------------------------


def _xml(package, text):
    return (
        '<?xml version="1.0"?><hierarchy rotation="0">'
        f'<node text="{text}" resource-id="" class="android.widget.TextView" package="{package}" '
        'content-desc="" clickable="true" enabled="true" visible-to-user="true" bounds="[0,0][540,100]"/>'
        "</hierarchy>"
    )


class FakeAdb:
    def __init__(self, focus_lines, xmls):
        self.focus_lines, self.xmls = list(focus_lines), list(xmls)
        self.dumps = 0

    def __call__(self, serial, *args, timeout=30.0, binary=False):
        cmd = " ".join(args)
        if "dumpsys window" in cmd:
            line = self.focus_lines.pop(0) if len(self.focus_lines) > 1 else self.focus_lines[0]
            return f"  {line}\n"
        if "wm size" in cmd:
            return "Physical size: 1080x2400\n"
        if "uiautomator dump" in cmd:
            self.dumps += 1
            return ""
        if cmd.startswith("exec-out cat"):
            return self.xmls.pop(0) if len(self.xmls) > 1 else self.xmls[0]
        raise AssertionError(f"unexpected adb call: {cmd}")


def _use_fake_adb(monkeypatch, fake):
    """Route every adb call through `fake`: adb.py calls adb() directly and
    also through adb_shell.shell(), which looks adb up in its own module."""
    monkeypatch.setattr(adb_mod, "adb", fake)
    monkeypatch.setattr(adb_shell_mod, "adb", fake)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(adb_mod.time, "sleep", lambda s: None)


def test_adb_device_settles_after_cold_start_and_observation_dumps_fresh(monkeypatch, no_sleep):
    fake = FakeAdb(
        ["mCurrentFocus=null", "mCurrentFocus=Window{1 u0 Splash Screen com.labs.snackorders}",
         f"mCurrentFocus={APP}"],
        [_xml("com.google.android.apps.nexuslauncher", "Jetsnack"),
         _xml("com.labs.snackorders", "Picks"), _xml("com.labs.snackorders", "Picks")],
    )
    _use_fake_adb(monkeypatch, fake)
    device = AdbDevice("emulator-0")

    result = device.settle(max_s=6.0)
    assert result.reason == "stable"
    assert '"Picks"' in result.tree and "packages: com.labs.snackorders" in result.tree
    n = fake.dumps
    device.ui_tree()
    assert fake.dumps == n + 1  # no reuse: the observation's tree matches its screenshot moment


def test_adb_device_compares_with_the_window_the_previous_settle_ended_on(monkeypatch, no_sleep):
    fake = FakeAdb([f"mCurrentFocus={APP}"], [_xml("com.labs.snackorders", "Picks")])
    _use_fake_adb(monkeypatch, fake)
    device = AdbDevice("emulator-0")
    assert device.settle(max_s=6.0).reason == "stable"  # nothing known before the first settle
    n = fake.dumps
    assert device.settle(max_s=6.0).reason == "focus_only"
    assert fake.dumps == n  # in-app action on an unchanged window: no dump
    fake.focus_lines = [f"mCurrentFocus={LAUNCHER}"]
    fake.xmls = [_xml("com.google.android.apps.nexuslauncher", "Jetsnack")]
    assert device.settle(max_s=6.0).reason == "stable"  # window changed: full check again
    assert fake.dumps == n + 2


def test_adb_device_forgets_the_window_after_a_cap_with_focus_null(monkeypatch, no_sleep):
    fake = FakeAdb([f"mCurrentFocus={APP}", "mCurrentFocus=null", f"mCurrentFocus={APP}"],
                   [_xml("com.labs.snackorders", "Picks")])
    _use_fake_adb(monkeypatch, fake)
    device = AdbDevice("emulator-0")
    device.settle(max_s=6.0)  # consumes the first APP read and more; reset the script
    fake.focus_lines = ["mCurrentFocus=null", f"mCurrentFocus={APP}"]
    capped = device.settle(max_s=0.0)
    assert (capped.reason, capped.focus) == ("cap", None)
    assert device.settle(max_s=6.0).reason == "stable"  # unknown before -> full check


def test_adb_device_keeps_the_window_after_a_cap_with_focus_on_it(monkeypatch, no_sleep):
    """A cold start can cap while the first dump of the new app is in flight;
    the next in-app action should still get the focus-only path."""
    fake = FakeAdb([f"mCurrentFocus={APP}"], [_xml("com.labs.snackorders", "Picks")])
    _use_fake_adb(monkeypatch, fake)
    device = AdbDevice("emulator-0")
    capped = device.settle(max_s=0.0)
    assert (capped.reason, capped.focus) == ("cap", APP)
    assert device.settle(max_s=6.0).reason == "focus_only"


@pytest.mark.parametrize("line, expected", [
    ("mCurrentFocus=null", None),
    ("mCurrentFocus=Window{1 u0 Splash Screen com.labs.snackorders}", None),
    (f"mCurrentFocus={APP}", APP),
    ("", None),  # no line at all (grep found nothing)
])
def test_focused_window_parsing(monkeypatch, line, expected):
    _use_fake_adb(monkeypatch, FakeAdb([line], ["<hierarchy/>"]))
    assert AdbDevice("emulator-0").focused_window() == expected


# --- the agent loop ---------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    return Config(runs_dir=str(tmp_path), max_steps=10, budget_usd=2.0)


def _approve(_req):
    return ConfirmationDecision.APPROVE


class Recorder:
    def __init__(self):
        self.steps = []

    def __call__(self, record, png, tree):
        self.steps.append(record)


class SettlingDevice(FakeDevice):
    """Logs actions, settles and screenshots in order."""

    def __init__(self, settle_result=None, settle_error=None, **kw):
        super().__init__(**kw)
        self.log = []
        self.settle_result = settle_result or SettleResult(1.25, "stable", "tree")
        self.settle_error = settle_error
        self.settle_caps = []

    def screenshot(self):
        self.log.append("screenshot")
        return super().screenshot()

    def _act(self, *call):
        self.log.append(call[0])
        super()._act(*call)

    def settle(self, max_s):
        self.log.append("settle")
        self.settle_caps.append(max_s)
        if self.settle_error:
            raise self.settle_error
        return self.settle_result


def test_loop_settles_after_each_ui_action_before_the_next_screenshot(config):
    device = SettlingDevice(queries={"sms.list": {"rows": []}})
    model = ScriptedModel([
        tool("tap", x=1, y=2),
        tool("query_structured", name="sms.list", params={}),
        tool("back"),
        tool("finish", verdict="done", summary="ok"),
    ])
    rec = Recorder()
    out = run_agent("x", device, config, _approve, rec, model_client=model)
    assert out.termination_reason is TerminationReason.FINISHED
    # no settle after the query (it cannot change the screen) or the finish
    assert device.log == ["screenshot", "tap", "settle", "screenshot", "screenshot", "back", "settle", "screenshot"]
    assert device.settle_caps == [8.0, 8.0]
    metas = [r.meta for r in rec.steps]
    assert metas[0]["settle"] == "stable" and metas[0]["settle_s"] >= 0
    assert metas[1] == {} and metas[3] == {}
    assert metas[2]["settle"] == "stable"


def test_loop_without_device_settle_records_zero_and_does_not_wait(config):
    device = FakeDevice()
    model = ScriptedModel([tool("tap", x=1, y=2), tool("finish", verdict="done", summary="ok")])
    rec = Recorder()
    run_agent("x", device, config, _approve, rec, model_client=model)
    assert rec.steps[0].meta == {"settle_s": 0.0}
    assert rec.steps[1].meta == {}


def test_loop_does_not_settle_after_a_failed_action(config):
    device = SettlingDevice(fail_actions=1)
    model = ScriptedModel([tool("tap", x=1, y=2), tool("finish", verdict="done", summary="ok")])
    rec = Recorder()
    run_agent("x", device, config, _approve, rec, model_client=model)
    assert "settle" not in device.log
    assert rec.steps[0].meta == {}


def test_settle_device_error_is_recorded_and_the_run_continues(config):
    device = SettlingDevice(settle_error=DeviceError("dumpsys failed"))
    model = ScriptedModel([tool("tap", x=1, y=2), tool("finish", verdict="done", summary="ok")])
    rec = Recorder()
    out = run_agent("x", device, config, _approve, rec, model_client=model)
    assert out.termination_reason is TerminationReason.FINISHED
    assert rec.steps[0].meta["settle"] == "error"
    assert device.log[-1] == "screenshot"  # observed anyway


def test_settle_cap_never_exceeds_the_remaining_wall_clock(tmp_path):
    t = [0.0]
    cfg = Config(runs_dir=str(tmp_path), max_steps=10, budget_usd=2.0, wall_clock_s=100)
    device = SettlingDevice()
    model = ScriptedModel([tool("tap", x=1, y=2), tool("finish", verdict="done", summary="ok")])

    def clock():
        t[0] += 24.0  # each clock read eats wall time
        return t[0]

    run_agent("x", device, cfg, _approve, Recorder(), model_client=model, clock=clock)
    assert device.settle_caps and 0.0 <= device.settle_caps[0] < 8.0


def test_settle_runs_through_the_runners_gated_device(config):
    """Production runs hand the loop a runner._GatedDevice, not the device itself."""
    from harness.runner import _GatedDevice

    inner = SettlingDevice()
    model = ScriptedModel([tool("tap", x=1, y=2), tool("finish", verdict="done", summary="ok")])
    rec = Recorder()
    run_agent("x", _GatedDevice(inner), config, _approve, rec, model_client=model)
    assert inner.log == ["screenshot", "tap", "settle", "screenshot"]
    assert rec.steps[0].meta["settle"] == "stable"


def test_gated_device_over_a_device_without_settle_records_zero(config):
    from harness.runner import _GatedDevice

    model = ScriptedModel([tool("tap", x=1, y=2), tool("finish", verdict="done", summary="ok")])
    rec = Recorder()
    run_agent("x", _GatedDevice(FakeDevice()), config, _approve, rec, model_client=model)
    assert rec.steps[0].meta == {"settle_s": 0.0}
