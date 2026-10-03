"""Batched seed script: generation, output parsing and apply() dispatch."""

import shlex
from pathlib import Path

import pytest

import harness.device.inspect as ins
from harness.device.inspect import AdbError, Inspector, parse_seed_failures, parse_seed_output, seed_script
from harness.seed.generator import SeedContact, SeedEvent, apply, generate_plan

CONTACTS = [
    SeedContact("Ana O'Brien", "(206) 555-0101", "ana@example.com"),
    SeedContact("Bilal $Haddad", "", ""),
]
EVENTS = [SeedEvent("Dentist; rm -rf /", 1800000000000, 1800003600000, "Pier 9")]


def test_script_quotes_plan_strings_and_expands_only_script_vars():
    script = seed_script(CONTACTS, EVENTS, "labs-seed-1")
    tokens = [t for line in script.splitlines() for t in shlex.split(line.rstrip("&"))]
    # every plan string survives shell parsing as exactly one bind token
    assert "data1:s:Ana O'Brien" in tokens
    assert "data1:s:Bilal $Haddad" in tokens  # a literal $, not an expansion
    assert "title:s:Dentist; rm -rf /" in tokens
    assert "eventLocation:s:Pier 9" in tokens
    assert "raw_contact_id:l:$RID" in script and "calendar_id:l:$CAL" in script
    assert script.startswith("set -e\n") and script.rstrip().endswith('wait $P_e0 || echo "chain=e0 rc=$?"')


def test_script_uses_unique_markers_for_id_lookup_and_skips_empty_fields():
    script = seed_script(CONTACTS, EVENTS, "labs-seed-1")
    assert "sourceid:s:labs-seed-1-c0" in script and "sourceid:s:labs-seed-1-c1" in script
    assert "uid2445:s:labs-seed-1-e0" in script
    assert script.count("--where") == 5  # 2 contacts + calendar lookup x2 + 1 event
    assert script.count(ins.MIME_PHONE) == 1 and script.count(ins.MIME_EMAIL) == 1
    assert 'echo "contact=1:$RID"' in script and 'echo "event=0:$EID"' in script


def test_script_without_events_has_no_calendar_section():
    script = seed_script(CONTACTS, [], "m")
    assert "calendar" not in script
    assert sum(l.endswith(") &") for l in script.splitlines()) == 2


def test_every_chain_is_waited_on_by_pid_with_its_status_reported():
    lines = seed_script(CONTACTS, EVENTS, "m").splitlines()
    assert "P_c0=$!" in lines and "P_c1=$!" in lines and "P_e0=$!" in lines
    assert 'wait $P_c0 || echo "chain=c0 rc=$?"' in lines
    assert 'wait $P_e0 || echo "chain=e0 rc=$?"' in lines
    assert "wait\n" not in "\n".join(lines) + "\n" and not any(l.strip() == "wait" for l in lines)
    # each pid is recorded right after its launch line
    for name in ("c0", "c1", "e0"):
        i = lines.index(f"P_{name}=$!")
        assert lines[i - 1].endswith(") &")


def test_concurrency_is_capped_in_batches():
    contacts = [SeedContact(f"Name {i}", "", "") for i in range(10)]
    lines = seed_script(contacts, [], "m", max_parallel=4).splitlines()
    launches = [i for i, l in enumerate(lines) if l.endswith(") &")]
    waits = [i for i, l in enumerate(lines) if l.startswith("wait $P_")]
    assert len(launches) == len(waits) == 10
    # batches of 4: the 5th launch comes after the first 4 waits
    assert waits[3] < launches[4] and waits[7] < launches[8]
    # never more than 4 chains in flight
    in_flight = 0
    for l in lines:
        in_flight += l.endswith(") &") - l.startswith("wait $P_")
        assert 0 <= in_flight <= 4
    with pytest.raises(ValueError):
        seed_script(contacts, [], "m", max_parallel=0)


def test_parse_failures_picks_only_chain_status_lines():
    out = "contact=0:1\nchain=c1 rc=1\nRow: 0 _id=chain=c9 rc=3\nchain=e0 rc=255\n"
    assert parse_seed_failures(out) == ["chain=c1 rc=1", "chain=e0 rc=255"]


def test_apply_batch_reports_failed_chains_with_their_status(no_adb):
    insp = ScriptedInspector("contact=0:1\nchain=c1 rc=1\nevent=0:5\n")
    with pytest.raises(AdbError, match=r"chains failed \(chain=c1 rc=1\)"):
        insp.apply_batch(CONTACTS, EVENTS, [("/sdcard/x.md", b"x")])
    assert insp.pushed == []


def test_script_rejects_marker_that_could_break_quoting():
    with pytest.raises(ValueError):
        seed_script(CONTACTS, [], "x' OR 1=1")


def test_parse_output_orders_by_position_and_ignores_noise():
    out = "event=1:30\nRow: 0 _id=5\ncontact=1:12\ncontact=0:11\nevent=0:29\ncontact=7:99\ncontact=x:1\n"
    assert parse_seed_output(out, 2, 2) == (["11", "12"], ["29", "30"])


def test_parse_output_marks_missing_ids_empty():
    assert parse_seed_output("contact=0:4\n", 2, 1) == (["4", ""], [""])
    assert parse_seed_output("contact=0:abc\ncontact=1:\n", 2, 0) == (["", ""], [])


class ScriptedInspector(Inspector):
    """Inspector whose device is a recorder: returns canned script output."""

    def __init__(self, output: str) -> None:
        super().__init__("fake")
        self.output = output
        self.shells: list[list[str]] = []
        self.pushed: list[tuple[str, bytes]] = []

    def shell(self, argv, timeout=30.0):
        self.shells.append(argv)
        return self.output if argv[:1] == ["sh"] else ""

    def push_file(self, path, content):
        self.pushed.append((path, content))


@pytest.fixture
def no_adb(monkeypatch):
    """Records adb pushes as (args, pushed file content)."""
    pushes = []

    def fake_run_adb(serial, args, timeout=30.0):
        pushes.append((args, Path(args[1]).read_text() if args[0] == "push" else None))
        return b""

    monkeypatch.setattr(ins, "run_adb", fake_run_adb)
    return pushes


def test_apply_uses_apply_batch_on_a_real_inspector(no_adb):
    plan = generate_plan(5, with_events=True)
    out = "".join(f"contact={i}:{100 + i}\n" for i in range(6)) + "".join(f"event={i}:{200 + i}\n" for i in range(3))
    insp = ScriptedInspector(out)
    applied = apply(plan, insp)
    assert applied.contact_ids == [str(100 + i) for i in range(6)]
    assert applied.event_ids == ["200", "201", "202"]
    assert applied.files == [f"{insp.markor_dir}/{n.filename}" for n in plan.notes]
    assert insp.pushed == [(f"{insp.markor_dir}/{n.filename}", n.content.encode()) for n in plan.notes]
    ((args, script),) = no_adb
    assert args[0] == "push" and args[2] == ins.SEED_SCRIPT_PATH
    assert script.startswith("set -e\n") and all(c.name in script for c in plan.contacts)
    assert not Path(args[1]).exists()  # host temp file removed
    assert insp.shells.index(["sh", ins.SEED_SCRIPT_PATH]) < insp.shells.index(["rm", "-f", ins.SEED_SCRIPT_PATH])


@pytest.mark.parametrize("marker", ["Error while accessing provider", "Exception:", "java.lang."])
def test_apply_batch_raises_on_provider_error(no_adb, marker):
    insp = ScriptedInspector(f"{marker} something went wrong\ncontact=0:1\n")
    with pytest.raises(AdbError, match="seed script"):
        insp.apply_batch(CONTACTS[:1], [], [("/sdcard/x.md", b"x")])
    assert insp.pushed == []


def test_apply_batch_shell_failure_propagates_and_still_cleans_up(no_adb):
    class Failing(ScriptedInspector):
        def shell(self, argv, timeout=30.0):
            self.shells.append(argv)
            if argv[:1] == ["sh"]:
                raise AdbError("adb: device offline")
            return ""

    insp = Failing("")
    with pytest.raises(AdbError, match="device offline"):
        insp.apply_batch(CONTACTS, [], [("/sdcard/x.md", b"x")])
    assert ["rm", "-f", ins.SEED_SCRIPT_PATH] in insp.shells and insp.pushed == []


def test_apply_batch_ignores_failure_to_remove_script(no_adb):
    class RmFails(ScriptedInspector):
        def shell(self, argv, timeout=30.0):
            if argv[:1] == ["rm"]:
                raise AdbError("rm: permission denied")
            return super().shell(argv, timeout)

    insp = RmFails("contact=0:3\ncontact=1:4\n")
    assert insp.apply_batch(CONTACTS, [], []) == (["3", "4"], [])


@pytest.mark.parametrize("out,msg", [
    ("contact=0:1\n", "1/2 contact ids and 0/1 event ids"),
    ("contact=0:1\ncontact=1:2\n", "2/2 contact ids and 0/1 event ids"),
])
def test_apply_batch_raises_when_an_id_is_missing(no_adb, out, msg):
    insp = ScriptedInspector(out)
    with pytest.raises(AdbError, match=msg):
        insp.apply_batch(CONTACTS, EVENTS, [("/sdcard/x.md", b"x")])
    assert insp.pushed == []  # notes are not written when seeding failed
    assert ["rm", "-f", ins.SEED_SCRIPT_PATH] in insp.shells  # script cleaned up anyway


def test_apply_batch_path_runs_extras_after_the_batch(no_adb):
    class Extra:
        def apply(self, target):
            target.shells.append(["extra", target.serial])

    plan = generate_plan(5, with_events=True)
    plan.extras.append(Extra())
    insp = ScriptedInspector("".join(f"contact={i}:{i}\n" for i in range(6)) + "".join(f"event={i}:{i}\n" for i in range(3)))
    apply(plan, insp)
    assert insp.shells[-1] == ["extra", "fake"] and ["sh", ins.SEED_SCRIPT_PATH] in insp.shells
