import json
import random
from pathlib import Path

import pytest

from harness.contracts import DeviceError
from harness.device import adb_shell
from harness.seed import sample_app as sa
from harness.seed.generator import generate_plan
from harness.seed.sample_app import (
    AdbSampleApp, FakeSnackOrders, SampleAppSeed, SampleSeed, SeedCartLine, SeedOrder, SeedOrderItem, SeedProduct,
    attach, create_schema, load_seed, make_sample_seed, open_db, seed_of,
)
from harness.tasks import sample_app as tasks
from tests.fakes import FakeInspector


def test_make_sample_seed_is_deterministic_and_has_distinct_order_totals():
    for seed in range(40):
        a = make_sample_seed(random.Random(seed))
        b = make_sample_seed(random.Random(seed))
        assert a == b
        assert len(a.products) == len(sa.CATALOGUE)
        assert len({p.price_cents for p in a.products}) == len(a.products)
        totals = [o.total_cents for o in a.orders]
        assert len(set(totals)) == len(totals) == 3
        assert len(a.cart) == 2 and len({c.product_id for c in a.cart}) == 2
        assert len({c.quantity for c in a.cart}) == 2 and all(1 <= c.quantity <= 3 for c in a.cart)
        newest = a.orders_newest_first()
        assert [o.placed_at for o in newest] == sorted((o.placed_at for o in a.orders), reverse=True)
        assert newest[0].status == "SHIPPED"
    assert make_sample_seed(random.Random(1)) != make_sample_seed(random.Random(2))


def test_seed_validation():
    product = SeedProduct(1, "Cupcake", "", 299, "cupcake", "Bakery")
    with pytest.raises(ValueError):
        SampleSeed((product,), variant="C")
    with pytest.raises(ValueError):
        SampleSeed((product,), provider_mode="broken")


def test_to_json_follows_seed_format_and_carries_no_totals():
    seed = make_sample_seed(random.Random(3), variant="B", provider_mode="stale")
    doc = seed.to_json()
    assert set(doc) == {"variant", "provider_mode", "shipping_cents", "interruptions", "products", "cart", "orders"}
    assert doc["variant"] == "B" and doc["provider_mode"] == "stale"
    assert doc["interruptions"] == {"notification_prompt": False, "promo_dialog": False}
    assert {frozenset(p) for p in doc["products"]} == {
        frozenset({"id", "name", "tagline", "price_cents", "image", "collection"})
    }
    for o in doc["orders"]:
        assert set(o) == {"id", "placed_at", "status", "shipping_cents", "items"}
        assert "total_cents" not in o and "subtotal_cents" not in o  # the app computes them
        for i in o["items"]:
            assert set(i) == {"product_id", "quantity", "unit_price_cents", "name"}
    json.dumps(doc)  # serialisable


def _db_rows(conn, sql):
    return conn.execute(sql).fetchall()


def test_load_seed_builds_the_database_like_the_app():
    seed = make_sample_seed(random.Random(5))
    conn = open_db()
    create_schema(conn)
    load_seed(conn, seed.to_json())
    orders = {row[0]: row for row in _db_rows(conn, "SELECT id, placed_at, status, subtotal_cents, shipping_cents, total_cents FROM orders")}
    for o in seed.orders:
        assert orders[o.id] == (o.id, o.placed_at, o.status, o.subtotal_cents, o.shipping_cents, o.total_cents)
        items = _db_rows(conn, f"SELECT product_id, product_name, quantity, unit_price_cents FROM order_items WHERE order_id={o.id} ORDER BY id")
        assert items == [(i.product_id, i.name, i.quantity, i.unit_price_cents) for i in o.items]
    assert dict(_db_rows(conn, "SELECT product_id, quantity FROM cart_items")) == {c.product_id: c.quantity for c in seed.cart}
    settings = dict(_db_rows(conn, "SELECT key, value FROM settings"))
    assert settings == {"variant": "A", "provider_mode": "full", "shipping_cents": "369",
                        "notification_prompt": "off", "promo_dialog": "off"}
    # A new order gets the next id after the seeded ones, as AUTOINCREMENT gives the app.
    new_id = sa.insert_order(conn, None, 1, "PLACED", 369, [(1, "Cupcake", 1, 299)])
    assert new_id == max(o.id for o in seed.orders) + 1
    # Re-seeding wipes everything, including the autoincrement counter.
    load_seed(conn, seed.to_json())
    assert _db_rows(conn, "SELECT COUNT(*) FROM orders") == [(len(seed.orders),)]
    assert _db_rows(conn, "SELECT COUNT(*) FROM sqlite_sequence WHERE name='orders' AND seq > %d" % max(o.id for o in seed.orders)) == [(0,)]


def test_load_seed_rejects_bad_documents_like_the_app_and_keeps_the_old_data():
    conn = open_db()
    create_schema(conn)
    good = make_sample_seed(random.Random(2)).to_json()
    load_seed(conn, good)
    before = conn.serialize()
    products = [{"id": 1, "name": "Cupcake", "price_cents": 299}]
    bad_docs = [
        {"products": []},
        {"products": products, "cart": [{"product_id": 9, "quantity": 1}]},
        {"products": products, "cart": [{"product_id": 1, "quantity": 0}]},
        {"products": products, "orders": [{"placed_at": 1, "items": []}]},
        {"products": products, "orders": [{"placed_at": 1, "items": [{"product_id": 9, "quantity": 1}]}]},
        {"products": [{"id": 1, "name": "No price"}]},  # required key missing
        {"products": products, "orders": [{"items": [{"product_id": 1, "quantity": 1}]}]},  # no placed_at
    ]
    for doc in bad_docs:
        with pytest.raises(ValueError):
            load_seed(conn, doc)
        assert conn.serialize() == before, doc  # rejected seeds roll back
    conn = open_db()
    create_schema(conn)
    # Defaults: name and price fall back to the product's.
    load_seed(conn, {"products": products, "orders": [{"placed_at": 1, "items": [{"product_id": 1, "quantity": 2}]}]})
    assert _db_rows(conn, "SELECT product_name, unit_price_cents, quantity FROM order_items") == [("Cupcake", 299, 2)]
    assert _db_rows(conn, "SELECT subtotal_cents, shipping_cents, total_cents FROM orders") == [(598, 369, 967)]


def test_fake_app_applies_seed_and_round_trips_database_images():
    fake = FakeSnackOrders()
    seed = make_sample_seed(random.Random(8))
    fake.push_seed(json.dumps(seed.to_json()).encode())
    conn = open_db(fake.pull_db())
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone() == (3,)
    with conn:
        conn.execute("DELETE FROM cart_items")
    fake.push_db(conn.serialize())
    assert open_db(fake.pull_db()).execute("SELECT COUNT(*) FROM cart_items").fetchone() == (0,)
    with pytest.raises(DeviceError):
        fake.push_db(b"not a database")
    with pytest.raises(DeviceError):
        open_db(b"garbage")
    # A rejected seed surfaces as DeviceError (like the device handle) and leaves the data in place.
    before = fake.pull_db()
    with pytest.raises(DeviceError, match="rejected"):
        fake.push_seed(b'{"products": []}')
    with pytest.raises(DeviceError, match="rejected"):
        fake.push_seed(b"not json")
    assert fake.pull_db() == before and len(fake.seeds) == 1


def test_sample_app_seed_attaches_fake_handle_to_targets_without_serial():
    insp = FakeInspector()
    plan = generate_plan(4, tasks.TASKS[0])
    extra = seed_of(plan)
    assert isinstance(extra, SampleAppSeed) and extra.handle is None
    with pytest.raises(DeviceError):
        extra.read_db()
    extra.apply(insp)
    assert isinstance(insp.sample_app, FakeSnackOrders)
    assert extra.read_db().execute("SELECT COUNT(*) FROM orders").fetchone() == (3,)
    # Same target, same handle: the verifier sees what the seed wrote.
    again = SampleAppSeed(extra.seed)
    again.apply(insp)
    assert again.handle is insp.sample_app
    with pytest.raises(LookupError):
        seed_of(generate_plan(4))


def test_attach_and_equality_ignore_the_device_handle():
    plan = generate_plan(2)
    extra = attach(plan, make_sample_seed(random.Random(1)))
    assert plan.extras == [extra]
    other = SampleAppSeed(make_sample_seed(random.Random(1)))
    other.handle = object()
    assert extra == other
    assert "handle" not in repr(extra)


class _Target:
    """Inspector-like: a serial and a shell() that runs through adb."""

    def __init__(self, serial):
        self.serial = serial

    def shell(self, argv, timeout=30.0):
        import shlex

        from harness.device.inspect import run_adb

        return run_adb(self.serial, ["shell", " ".join(shlex.quote(a) for a in argv)], timeout).decode()


class RecordedAdb:
    """Stands in for adb_shell.adb: records calls, scripts replies, and keeps
    the bytes of every pushed file (the temp file is gone afterwards)."""

    def __init__(self, files_after_apply=(), db=b"SQLite format 3\x00" + b"\x00" * 64):
        self.calls: list[tuple] = []
        self.files = set(files_after_apply)
        self.db = db
        self.pushed: list[bytes] = []

    def __call__(self, serial, *args, timeout=30.0, binary=False):
        self.calls.append((serial, *args))
        if args[0] == "push":
            self.pushed.append(Path(args[1]).read_bytes())
        if args[0] == "exec-out":
            return self.db
        if args[0] == "shell" and "test -e" in args[1]:
            rel = args[1].split("test -e ", 1)[1].split(" ", 1)[0]
            return b"yes\n" if rel in self.files else b"no\n"
        return b""


def test_adb_handle_pushes_seed_through_run_as_and_applies_it(monkeypatch):
    rec = RecordedAdb()
    monkeypatch.setattr(adb_shell, "adb", rec)
    app = AdbSampleApp(_Target("emulator-5586"))
    seed = make_sample_seed(random.Random(9))
    app.push_seed(json.dumps(seed.to_json()).encode())
    assert len(rec.pushed) == 1 and json.loads(rec.pushed[0]) == seed.to_json()
    assert not any(k in rec.pushed[0] for k in (b"expected", b"total_cents", b"subtotal"))
    shells = [c[2] for c in rec.calls if c[1] == "shell"]
    assert all(c[0] == "emulator-5586" for c in rec.calls)
    assert shells[0] == f"am force-stop {sa.PACKAGE}"
    assert f"run-as {sa.PACKAGE} rm -f {sa.SEED_REJECTED_RELPATH}" in shells
    push = next(c for c in rec.calls if c[1] == "push")
    assert push[3] == sa.DEVICE_TMP
    assert f"run-as {sa.PACKAGE} cp {sa.DEVICE_TMP} {sa.SEED_RELPATH}" in shells
    assert f"rm -f {sa.DEVICE_TMP}" in shells
    assert f"content query --uri {sa.PROVIDER_ORDERS_URI}" in shells
    assert shells.index(f"content query --uri {sa.PROVIDER_ORDERS_URI}") > shells.index(
        f"run-as {sa.PACKAGE} cp {sa.DEVICE_TMP} {sa.SEED_RELPATH}")
    assert shells[-1].startswith("run-as") and "test -e" in shells[-1]
    # Cold start for the agent: the app is stopped again after the provider applied the seed.
    query_at = shells.index(f"content query --uri {sa.PROVIDER_ORDERS_URI}")
    assert shells[query_at + 1] == f"am force-stop {sa.PACKAGE}"


def test_adb_handle_reports_rejected_or_pending_seed(monkeypatch):
    rec = RecordedAdb(files_after_apply={sa.SEED_REJECTED_RELPATH})
    monkeypatch.setattr(adb_shell, "adb", rec)
    with pytest.raises(DeviceError, match="rejected"):
        AdbSampleApp(_Target("s")).push_seed(b"{}")
    rec = RecordedAdb(files_after_apply={sa.SEED_RELPATH})
    monkeypatch.setattr(adb_shell, "adb", rec)
    with pytest.raises(DeviceError, match="still waiting"):
        AdbSampleApp(_Target("s")).push_seed(b"{}")


def test_adb_handle_pulls_and_pushes_the_database(monkeypatch):
    rec = RecordedAdb()
    monkeypatch.setattr(adb_shell, "adb", rec)
    app = AdbSampleApp(_Target("s"))
    assert app.pull_db().startswith(sa.SQLITE_MAGIC)
    assert rec.calls[-1] == ("s", "exec-out", "run-as", sa.PACKAGE, "cat", sa.DB_RELPATH)
    app.push_db(sa.SQLITE_MAGIC)
    shells = [c[2] for c in rec.calls if c[1] == "shell"]
    assert f"run-as {sa.PACKAGE} cp {sa.DEVICE_TMP} {sa.DB_RELPATH}" in shells
    assert f"run-as {sa.PACKAGE} rm -f {sa.DB_RELPATH}-journal" == shells[-1]
    rec.db = b"Permission denied"
    with pytest.raises(DeviceError, match="not a SQLite database"):
        app.pull_db()


def test_seed_applies_over_adb_when_target_has_a_serial(monkeypatch):
    rec = RecordedAdb()
    monkeypatch.setattr(adb_shell, "adb", rec)

    target = _Target("emulator-5586")
    extra = SampleAppSeed(make_sample_seed(random.Random(1)))
    extra.apply(target)
    assert isinstance(target.sample_app, AdbSampleApp) and target.sample_app.serial == "emulator-5586"
    assert target.sample_app.inspector is target
    assert any(c[1] == "push" for c in rec.calls)


def test_seed_order_totals():
    o = SeedOrder(1, 0, "DELIVERED", 369, (SeedOrderItem(1, "A", 2, 100), SeedOrderItem(2, "B", 1, 250)))
    assert (o.subtotal_cents, o.total_cents) == (450, 819)
    seed = SampleSeed((SeedProduct(1, "A", "", 100, "", ""), SeedProduct(2, "B", "", 250, "", "")),
                      cart=(SeedCartLine(1, 2), SeedCartLine(2, 1)))
    assert seed.cart_total_cents() == 819
