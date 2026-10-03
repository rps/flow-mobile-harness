"""Per-run data for the controlled sample app (com.labs.snackorders).

A SampleSeed is derived from the task's rng and rendered to the seed.json
format in sample-app/SEED_FORMAT.md. SampleAppSeed is the plan "extra": the
seed generator's apply() calls extra.apply(target) after the contacts and
notes. On a real Inspector (anything with a `serial`) the seed is pushed with
`adb shell run-as` and applied by querying the orders provider; on a fake
target an in-memory copy of the app's database stands in, so fake runs and
tests need no device.

The same object is the verifier's oracle handle: read_db() returns a
host-side SQLite copy of the app's database (SCHEMA.md, tier 1) and
write_db() pushes a modified copy back (self-test gold/decoy injection only).
Expected values never go to the device: they live in plan.expected.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.contracts import DeviceError
from harness.device.inspect import run_adb

PACKAGE = "com.labs.snackorders"
ACTIVITY = f"{PACKAGE}/com.example.jetsnack.ui.MainActivity"
APP_LABEL = "Jetsnack"  # launcher label (sample-app strings.xml app_name)
PROVIDER_ORDERS_URI = f"content://{PACKAGE}.provider/orders"
QUERY_NAME = "snackorders.orders"  # the agent-side query_structured name for the provider
DB_RELPATH = "databases/snackorders.db"
SEED_RELPATH = "files/seed.json"
SEED_REJECTED_RELPATH = "files/seed.rejected.json"
DEVICE_TMP = "/data/local/tmp/snackorders-harness.tmp"
SQLITE_MAGIC = b"SQLite format 3\x00"

DEFAULT_SHIPPING_CENTS = 369
VARIANTS = ("A", "B")
PROVIDER_MODES = ("full", "stale", "partial")

# (name, tagline, bundled image, Home-tab collection). Prices are per seed.
CATALOGUE = [
    ("Cupcake", "Vanilla, iced", "cupcake", "Bakery"),
    ("Donut", "Glazed ring", "donut", "Bakery"),
    ("Eclair", "Chocolate top", "eclair", "Bakery"),
    ("Gingerbread", "Spiced", "gingerbread", "Bakery"),
    ("Apple Pie", "Deep dish", "apple_pie", "Bakery"),
    ("Pretzels", "Salted", "pretzels", "Savoury"),
    ("Popcorn", "Butter", "popcorn", "Savoury"),
    ("Almonds", "Roasted", "almonds", "Savoury"),
    ("Chips", "Sea salt", "chips", "Savoury"),
    ("Mango", "Dried slices", "mango", "Fruit"),
    ("Kiwi", "Fresh", "kiwi", "Fruit"),
    ("Grapes", "Seedless", "grapes", "Fruit"),
]
FIRST_ORDER_ID = 1001
ORDERS_BASE_MS = 1_756_684_800_000  # 2026-09-01T00:00:00Z


# --- Seed model --------------------------------------------------------------


@dataclass(frozen=True)
class SeedProduct:
    id: int
    name: str
    tagline: str
    price_cents: int
    image: str
    collection: str
    # Catalogue facts shown in the UI (SCHEMA.md); empty/None = not stated.
    tags: tuple[str, ...] = ()
    serving_size: int | None = None
    delivery_days: int | None = None

    def to_json(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"id": self.id, "name": self.name, "tagline": self.tagline,
                               "price_cents": self.price_cents, "image": self.image, "collection": self.collection}
        if self.tags:
            doc["tags"] = list(self.tags)
        if self.serving_size is not None:
            doc["serving_size"] = self.serving_size
        if self.delivery_days is not None:
            doc["delivery_days"] = self.delivery_days
        return doc


@dataclass(frozen=True)
class SeedOrderItem:
    product_id: int
    name: str
    quantity: int
    unit_price_cents: int

    @property
    def line_cents(self) -> int:
        return self.quantity * self.unit_price_cents


@dataclass(frozen=True)
class SeedOrder:
    id: int
    placed_at: int
    status: str
    shipping_cents: int
    items: tuple[SeedOrderItem, ...]

    @property
    def subtotal_cents(self) -> int:
        return sum(i.line_cents for i in self.items)

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.shipping_cents


@dataclass(frozen=True)
class SeedCartLine:
    product_id: int
    quantity: int


@dataclass(frozen=True)
class SampleSeed:
    """What the app will hold after the seed is applied."""

    products: tuple[SeedProduct, ...]
    cart: tuple[SeedCartLine, ...] = ()
    orders: tuple[SeedOrder, ...] = ()
    variant: str = "A"
    provider_mode: str = "full"
    shipping_cents: int = DEFAULT_SHIPPING_CENTS
    notification_prompt: bool = False
    promo_dialog: bool = False

    def __post_init__(self) -> None:
        if self.variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        if self.provider_mode not in PROVIDER_MODES:
            raise ValueError(f"provider_mode must be one of {PROVIDER_MODES}")

    def product(self, product_id: int) -> SeedProduct:
        return next(p for p in self.products if p.id == product_id)

    def orders_newest_first(self) -> list[SeedOrder]:
        """The app's order: placed_at DESC, id DESC."""
        return sorted(self.orders, key=lambda o: (o.placed_at, o.id), reverse=True)

    def cart_total_cents(self) -> int:
        return sum(self.product(c.product_id).price_cents * c.quantity for c in self.cart) + self.shipping_cents

    def to_json(self) -> dict[str, Any]:
        """The seed.json document (SEED_FORMAT.md). Carries no expected values."""
        return {
            "variant": self.variant,
            "provider_mode": self.provider_mode,
            "shipping_cents": self.shipping_cents,
            "interruptions": {
                "notification_prompt": self.notification_prompt,
                "promo_dialog": self.promo_dialog,
            },
            "products": [p.to_json() for p in self.products],
            "cart": [{"product_id": c.product_id, "quantity": c.quantity} for c in self.cart],
            "orders": [
                {
                    "id": o.id, "placed_at": o.placed_at, "status": o.status, "shipping_cents": o.shipping_cents,
                    "items": [
                        {"product_id": i.product_id, "quantity": i.quantity,
                         "unit_price_cents": i.unit_price_cents, "name": i.name}
                        for i in o.items
                    ],
                }
                for o in self.orders
            ],
        }


def make_sample_seed(rng: random.Random, *, n_orders: int = 3, n_cart: int = 2, variant: str = "A",
                     provider_mode: str = "full") -> SampleSeed:
    """Catalogue with per-seed prices, `n_orders` past orders with pairwise
    distinct totals (so a wrong order is detectable), `n_cart` cart lines."""
    prices = rng.sample(range(149, 999), len(CATALOGUE))
    products = tuple(
        SeedProduct(i + 1, name, tagline, price, image, collection)
        for i, ((name, tagline, image, collection), price) in enumerate(zip(CATALOGUE, prices))
    )
    shipping = DEFAULT_SHIPPING_CENTS
    orders: list[SeedOrder] = []
    placed = ORDERS_BASE_MS + rng.randrange(0, 5) * 86_400_000
    for n in range(n_orders):
        while True:
            chosen = rng.sample(products, rng.randrange(1, 4))
            items = tuple(SeedOrderItem(p.id, p.name, rng.randrange(1, 4), p.price_cents) for p in chosen)
            status = "SHIPPED" if n == n_orders - 1 else "DELIVERED"
            order = SeedOrder(FIRST_ORDER_ID + n, placed, status, shipping, items)
            if order.total_cents not in {o.total_cents for o in orders}:
                break
        orders.append(order)
        placed += rng.randrange(2, 9) * 86_400_000 + rng.randrange(0, 86_400_000)
    # Distinct quantities, so a summary with the quantities swapped is detectable.
    quantities = rng.sample(range(1, max(3, n_cart) + 1), n_cart)
    cart = tuple(SeedCartLine(p.id, q) for p, q in zip(rng.sample(products, n_cart), quantities))
    return SampleSeed(products, cart, tuple(orders), variant, provider_mode, shipping)


# --- The app's database, host side ------------------------------------------


SCHEMA_SQL = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    tagline TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL,
    image TEXT NOT NULL DEFAULT '',
    collection TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    serving_size INTEGER,
    delivery_days INTEGER
);
CREATE TABLE cart_items (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0)
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    placed_at INTEGER NOT NULL,
    status TEXT NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    shipping_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL
);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def open_db(data: bytes | None = None) -> sqlite3.Connection:
    """An in-memory connection, loaded from a database image when given."""
    conn = sqlite3.connect(":memory:")
    if data is not None:
        if not data.startswith(SQLITE_MAGIC):
            raise DeviceError(f"not a SQLite database ({len(data)} bytes)")
        conn.deserialize(data)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def load_seed(conn: sqlite3.Connection, seed: dict[str, Any]) -> None:
    """Wipe and rebuild the database from a seed document the way the app's
    Store.loadSeed does (products required here: the built-in catalogue is
    not reproduced)."""
    products = seed.get("products")
    if not products:
        raise ValueError("seed must list products")
    shipping = int(seed.get("shipping_cents", DEFAULT_SHIPPING_CENTS))
    inter = seed.get("interruptions") or {}
    try:
        with conn:
            _load_seed_rows(conn, seed, products, shipping, inter)
    except sqlite3.IntegrityError as e:  # e.g. quantity <= 0; the transaction is rolled back
        raise ValueError(str(e)) from e
    except KeyError as e:  # a required field is missing
        raise ValueError(f"missing field {e}") from e


def _load_seed_rows(conn: sqlite3.Connection, seed: dict[str, Any], products: list[dict[str, Any]],
                    shipping: int, inter: dict[str, Any]) -> None:
    for table in ("order_items", "orders", "cart_items", "products", "settings"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.executemany("INSERT INTO settings(key, value) VALUES (?, ?)", [
        ("variant", seed.get("variant", "A")),
        ("provider_mode", seed.get("provider_mode", "full")),
        ("shipping_cents", str(shipping)),
        ("notification_prompt", "pending" if inter.get("notification_prompt") else "off"),
        ("promo_dialog", "pending" if inter.get("promo_dialog") else "off"),
    ])
    prices: dict[int, int] = {}
    names: dict[int, str] = {}
    for pos, p in enumerate(products):
        tags, serving_size, delivery_days = product_facts(p)
        conn.execute(
            "INSERT INTO products(id, name, tagline, price_cents, image, collection, position, tags, serving_size, "
            "delivery_days) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["name"], p.get("tagline", ""), p["price_cents"], p.get("image", ""),
             p.get("collection", ""), pos, tags, serving_size, delivery_days),
        )
        prices[p["id"]], names[p["id"]] = p["price_cents"], p["name"]
    for line in seed.get("cart") or []:
        if line["product_id"] not in prices:
            raise ValueError(f"cart: unknown product_id {line['product_id']}")
        conn.execute("INSERT INTO cart_items(product_id, quantity) VALUES (?, ?)",
                     (line["product_id"], line["quantity"]))
    for o in seed.get("orders") or []:
        items = [
            (i["product_id"], i.get("name", names.get(i["product_id"])), i["quantity"],
             i.get("unit_price_cents", prices.get(i["product_id"])))
            for i in o["items"]
        ]
        if not items or any(n is None or u is None for _, n, _, u in items):
            raise ValueError(f"order {o.get('id')}: items incomplete")
        insert_order(conn, o.get("id"), o["placed_at"], o.get("status", "DELIVERED"),
                     o.get("shipping_cents", shipping), items)


TAG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _org_json_string(value: Any) -> str:
    """What the app's JSONArray.getString returns for an element (measured on
    emulator-5586): strings as is, true/false and null spelled out, numbers as text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _org_json_int(value: Any, what: str) -> int:
    """What the app's JSONObject.getInt accepts (measured on emulator-5586):
    integers, numbers truncated toward zero, numeric strings ("4", "2.7");
    null, booleans and other strings are rejected."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{what} is not a number")
    if isinstance(value, int):
        return value  # exact; the float path below would round integers above 2**53
    try:
        number = float(value)
        if number != number:  # NaN
            raise ValueError
        return int(number)
    except (ValueError, OverflowError, TypeError):  # "four", inf / huge, lists and objects
        raise ValueError(f"{what} is not a number") from None


def product_facts(p: dict[str, Any]) -> tuple[str, int | None, int | None]:
    """(tags as stored, serving_size, delivery_days) the way Store.loadSeed
    reads them: a non-array `tags` means no tags; each tag must be lowercase
    words joined by "-"; present numbers must be positive. ValueError where the app rejects."""
    raw = p.get("tags")
    tags = [_org_json_string(t) for t in raw] if isinstance(raw, list) else []
    if not all(TAG_RE.fullmatch(t) for t in tags):
        raise ValueError(f"product {p.get('id')}: tags must be lowercase words joined by '-'")
    numbers: list[int | None] = []
    for key in ("serving_size", "delivery_days"):
        if key not in p:
            numbers.append(None)
            continue
        value = _org_json_int(p[key], f"product {p.get('id')}: {key}")
        if value <= 0:
            raise ValueError(f"product {p.get('id')}: {key} must be positive")
        numbers.append(value)
    return ",".join(tags), numbers[0], numbers[1]


def insert_order(conn: sqlite3.Connection, order_id: int | None, placed_at: int, status: str, shipping: int,
                 items: list[tuple[int, str, int, int]]) -> int:
    """Insert an order with (product_id, name, quantity, unit_price_cents)
    lines; totals computed as the app does. Returns the order id."""
    subtotal = sum(q * u for _, _, q, u in items)
    cur = conn.execute(
        "INSERT INTO orders(id, placed_at, status, subtotal_cents, shipping_cents, total_cents) VALUES (?,?,?,?,?,?)",
        (order_id, placed_at, status, subtotal, shipping, subtotal + shipping),
    )
    oid = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO order_items(order_id, product_id, product_name, quantity, unit_price_cents) VALUES (?,?,?,?,?)",
        [(oid, pid, name, q, u) for pid, name, q, u in items],
    )
    return oid


# --- Device handles ----------------------------------------------------------


class AdbSampleApp:
    """The real app over adb, through an Inspector-like target (`serial`,
    `shell(argv, timeout)`). Everything inside the app's private directory
    goes through `run-as`, which the debuggable APK allows."""

    def __init__(self, inspector: Any) -> None:
        self.inspector = inspector
        self.serial: str = inspector.serial

    def _shell(self, *args: str, timeout: float = 30.0) -> str:
        return self.inspector.shell(list(args), timeout)

    def _push_private(self, content: bytes, relpath: str) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
        try:
            run_adb(self.serial, ["push", f.name, DEVICE_TMP], timeout=60)
            self._shell("run-as", PACKAGE, "mkdir", "-p", str(Path(relpath).parent))
            self._shell("run-as", PACKAGE, "cp", DEVICE_TMP, relpath)
        finally:
            Path(f.name).unlink(missing_ok=True)
            try:
                self._shell("rm", "-f", DEVICE_TMP)
            except DeviceError:
                pass  # never mask the push/run-as error with the cleanup's

    def _private_exists(self, relpath: str) -> bool:
        out = self._shell("run-as", PACKAGE, "sh", "-c", f"test -e {relpath} && echo yes || echo no")
        return out.strip().endswith("yes")

    def force_stop(self) -> None:
        self._shell("am", "force-stop", PACKAGE)

    def push_seed(self, seed_json: bytes) -> None:
        """Force-stop, drop the file in files/, apply it through the provider
        (SCHEMA.md: a provider query applies a waiting seed), force-stop again
        so the agent gets a cold start. Raises if the app rejected it."""
        self.force_stop()
        self._shell("run-as", PACKAGE, "rm", "-f", SEED_REJECTED_RELPATH)
        self._push_private(seed_json, SEED_RELPATH)
        self._shell("content", "query", "--uri", PROVIDER_ORDERS_URI, timeout=60)
        self.force_stop()
        if self._private_exists(SEED_REJECTED_RELPATH):
            raise DeviceError(f"{PACKAGE} rejected the seed (files/seed.rejected.json; see logcat tag SnackOrders)")
        if self._private_exists(SEED_RELPATH):
            raise DeviceError(f"{PACKAGE} did not apply the seed: files/seed.json is still waiting")

    def pull_db(self) -> bytes:
        data = run_adb(self.serial, ["exec-out", "run-as", PACKAGE, "cat", DB_RELPATH], timeout=60)
        if not data.startswith(SQLITE_MAGIC):
            raise DeviceError(f"{PACKAGE}: {DB_RELPATH} is not a SQLite database ({len(data)} bytes)")
        return data

    def push_db(self, data: bytes) -> None:
        self.force_stop()
        self._push_private(data, DB_RELPATH)
        self._shell("run-as", PACKAGE, "rm", "-f", f"{DB_RELPATH}-journal")


class FakeSnackOrders:
    """In-memory stand-in: a seed replaces the database like the app does."""

    def __init__(self) -> None:
        self.seeds: list[dict[str, Any]] = []
        conn = open_db()
        create_schema(conn)
        self._image = conn.serialize()

    def push_seed(self, seed_json: bytes) -> None:
        """Like the app: a rejected seed leaves the database untouched; like
        the device handle, the rejection surfaces as DeviceError."""
        conn = open_db(self._image)
        try:
            seed = json.loads(seed_json)
            load_seed(conn, seed)
        except ValueError as e:
            raise DeviceError(f"{PACKAGE} rejected the seed: {e}") from e
        self._image = conn.serialize()
        self.seeds.append(seed)

    def pull_db(self) -> bytes:
        return self._image

    def push_db(self, data: bytes) -> None:
        if not data.startswith(SQLITE_MAGIC):
            raise DeviceError("not a SQLite database")
        self._image = data


def app_handle(target: Any) -> Any:
    """The sample-app handle for a seed target: a `sample_app` attribute if
    present, else adb for a target with a `serial`, else a new fake. The
    handle is stored on the target so the verifier reads the same state."""
    handle = getattr(target, "sample_app", None)
    if handle is None:
        handle = AdbSampleApp(target) if getattr(target, "serial", None) else FakeSnackOrders()
        target.sample_app = handle
    return handle


# --- The plan extra ----------------------------------------------------------


@dataclass
class SampleAppSeed:
    """plan.extras entry: pushes `seed` in apply() and keeps the handle for
    the verifier. Equality and repr cover the seed only."""

    seed: SampleSeed
    handle: Any = field(default=None, compare=False, repr=False)

    def apply(self, target: Any) -> None:
        self.handle = app_handle(target)
        self.handle.push_seed(json.dumps(self.seed.to_json()).encode())

    def _require_handle(self) -> Any:
        if self.handle is None:
            raise DeviceError("sample app seed was never applied; no device handle")
        return self.handle

    def read_db(self) -> sqlite3.Connection:
        """A host-side copy of the app's database as it is now."""
        return open_db(self._require_handle().pull_db())

    def write_db(self, change: Callable[[sqlite3.Connection], None]) -> None:
        """Pull, apply `change`, push back. Self-test injection only."""
        conn = self.read_db()
        with conn:
            change(conn)
        self._require_handle().push_db(conn.serialize())


def attach(plan: Any, seed: SampleSeed) -> SampleAppSeed:
    """Add a SampleAppSeed to plan.extras and return it."""
    extra = SampleAppSeed(seed)
    plan.extras.append(extra)
    return extra


def seed_of(plan: Any) -> SampleAppSeed:
    """The plan's SampleAppSeed; raises if the plan has none."""
    for extra in plan.extras:
        if isinstance(extra, SampleAppSeed):
            return extra
    raise LookupError("plan has no sample app seed")
