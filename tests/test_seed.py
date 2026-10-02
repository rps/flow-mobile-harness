import re

from harness.seed.generator import apply, generate_plan, make_seed
from harness.tasks import a_markor_note, b_contact_to_note, f_send_sms
from tests.fakes import FakeInspector


def test_same_seed_same_plan_and_different_seed_differs():
    for task in (None, a_markor_note.TASK, b_contact_to_note.TASK, f_send_sms.TASK):
        assert generate_plan(123, task) == generate_plan(123, task)
        assert generate_plan(123, task) != generate_plan(124, task)


def test_make_seed_varies():
    assert len({make_seed() for _ in range(20)}) > 1


def test_plan_shapes_are_plausible_and_unique():
    plan = generate_plan(7, with_events=True)
    assert len(plan.contacts) == 6 and len(plan.notes) == 2 and len(plan.events) == 3
    assert len({c.phone for c in plan.contacts}) == 6
    assert len({c.name for c in plan.contacts}) == 6
    for c in plan.contacts:
        assert re.fullmatch(r"\(\d{3}\) 555-01\d\d", c.phone)
        assert c.email.endswith((".com", ".org", ".net")) and "@" in c.email
    for e in plan.events:
        assert e.end_ms > e.start_ms
    assert all(n.filename.endswith(".md") and n.content for n in plan.notes)


def test_repr_does_not_show_expected():
    plan = generate_plan(5, b_contact_to_note.TASK)
    assert plan.expected["decoy_email"] in {c.email for c in plan.contacts}
    assert "decoy_email" not in repr(plan) and "expected" not in repr(plan)


def test_b_seeds_decoy_with_same_first_name_and_distinct_details():
    for seed in range(30):
        plan = generate_plan(seed, b_contact_to_note.TASK)
        e = plan.expected
        first = e["name"].split()[0]
        same_first = [c for c in plan.contacts if c.name.split()[0] == first]
        assert len(same_first) == 2
        assert e["decoy_phone"] != e["phone"] and e["decoy_email"] != e["email"]
        assert len({c.phone for c in plan.contacts}) == len(plan.contacts)


def test_apply_writes_plan_data_to_device():
    fake = FakeInspector()
    plan = generate_plan(11, with_events=True)
    applied = apply(plan, fake)
    contacts = list(fake.contacts_db.values())
    assert [(c.name, c.phones[0], c.emails[0]) for c in contacts] == [
        (c.name, c.phone, c.email) for c in plan.contacts
    ]
    assert sorted(e.title for e in fake.events_db.values()) == sorted(e.title for e in plan.events)
    for n in plan.notes:
        assert fake.files_db[f"{fake.markor_dir}/{n.filename}"] == n.content.encode()
    assert len(applied.contact_ids) == 6 and len(applied.event_ids) == 3 and len(applied.files) == 2


def test_apply_respects_markor_dir_override():
    fake = FakeInspector()
    plan = generate_plan(11)
    apply(plan, fake, markor_dir="/sdcard/notes/")
    assert all(p.startswith("/sdcard/notes/") and "//" not in p for p in fake.files_db)


def test_apply_never_writes_expected_only_values_or_seed():
    for task, keys in ((a_markor_note.TASK, ["title", "content"]),
                       (b_contact_to_note.TASK, ["title"]),
                       (f_send_sms.TASK, ["message"])):
        fake = FakeInspector()
        plan = generate_plan(987654321, task)
        apply(plan, fake)
        on_device = " ".join(
            [b.decode() for b in fake.files_db.values()]
            + [f"{c.name} {c.phones} {c.emails}" for c in fake.contacts_db.values()]
            + list(fake.files_db)
        )
        for k in keys:
            assert plan.expected[k] not in on_device, (task.id, k)
        assert str(plan.seed) not in on_device
