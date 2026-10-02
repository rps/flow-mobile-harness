#!/usr/bin/env python3
"""Command-line acceptance checks for the snackorders sample app.

Usage: python3 scripts/acceptance.py <adb-serial> [path/to/apk]

Needs adb on PATH (or ANDROID_HOME set) and sqlite3 on the host. Drives the UI through
`uiautomator dump` + `input tap`, pulls the database with run-as and checks it with sqlite3.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

PKG = "com.labs.snackorders"
ACTIVITY = f"{PKG}/com.example.jetsnack.ui.MainActivity"
PROVIDER = "content://com.labs.snackorders.provider/orders"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERIAL = sys.argv[1] if len(sys.argv) > 1 else None
APK = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "dist", "snackorders-debug.apk")
SDK = os.environ.get("ANDROID_HOME", os.path.expanduser("~/Library/Android/sdk"))
ADB = os.path.join(SDK, "platform-tools", "adb") if os.path.exists(os.path.join(SDK, "platform-tools", "adb")) else "adb"
TMP = tempfile.mkdtemp(prefix="snackorders-")

results = []


def adb(*args, check=True, binary=False):
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, check=check)
    return p.stdout if binary else p.stdout.decode(errors="replace")


def shell(cmd, check=True):
    return adb("shell", cmd, check=check)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def force_stop():
    shell(f"am force-stop {PKG}")
    time.sleep(1)


def start_app():
    # -S force-stops first so every launch is a cold start that re-checks the seed file
    shell(f"am start -S -W -n {ACTIVITY}")
    time.sleep(2.5)


def screen_size():
    m = re.search(r"(\d+)x(\d+)", shell("wm size"))
    return int(m.group(1)), int(m.group(2))


def tap_tab(index):
    """Bottom navigation. Unselected tabs show only an icon, whose content description is the tab name."""
    tap(desc=["HOME", "SEARCH", "MY CART", "PROFILE"][index])


def push_seed(seed):
    path = os.path.join(TMP, "seed.json")
    with open(path, "w") as f:
        json.dump(seed, f)
    adb("push", path, "/data/local/tmp/seed.json")
    shell(f"run-as {PKG} mkdir -p files")
    shell(f"run-as {PKG} cp /data/local/tmp/seed.json files/seed.json")
    shell("rm /data/local/tmp/seed.json")


def seed_file_state():
    out = shell(f"run-as {PKG} ls files", check=False)
    return out.split()


def pull_db():
    data = adb("exec-out", f"run-as {PKG} cat databases/snackorders.db", binary=True)
    path = os.path.join(TMP, f"db-{int(time.time() * 1000)}.db")
    with open(path, "wb") as f:
        f.write(data)
    return sqlite3.connect(path)


def q1(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def ui_dump():
    shell("uiautomator dump /sdcard/ui.xml")
    xml = shell("cat /sdcard/ui.xml")
    return ET.fromstring(xml[xml.index("<?xml"):] if "<?xml" in xml else xml)


def find_node(root, text=None, desc=None, contains=False):
    for node in root.iter("node"):
        t = node.get("text") or ""
        d = node.get("content-desc") or ""
        if text is not None and (text in t if contains else t == text):
            return node
        if desc is not None and (desc in d if contains else d == desc):
            return node
    return None


def ui_texts(root):
    return [n.get("text") for n in root.iter("node") if n.get("text")]


def tap_node(node):
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds")))
    shell(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
    time.sleep(1.5)


def tap(text=None, desc=None, contains=False, tries=5):
    for _ in range(tries):
        node = find_node(ui_dump(), text=text, desc=desc, contains=contains)
        if node is not None:
            tap_node(node)
            return True
        time.sleep(1)
    return False


def wait_for_text(text, tries=6, contains=True):
    for _ in range(tries):
        if find_node(ui_dump(), text=text, contains=contains) is not None:
            return True
        time.sleep(1)
    return False


def provider_rows(uri=PROVIDER):
    out = shell(f"content query --uri {uri}", check=False)
    rows = []
    for line in out.splitlines():
        if line.startswith("Row:"):
            rows.append(dict(kv.split("=", 1) for kv in re.findall(r"(\w+=[^,]*)", line.split(" ", 2)[2])))
    return rows


def app_alive():
    return bool(shell(f"pidof {PKG}", check=False).strip())


def base_seed(**overrides):
    with open(os.path.join(ROOT, "examples", "seed.json")) as f:
        seed = json.load(f)
    seed.update(overrides)
    return seed


def place_order_via_ui(checkout_label, place_label, done_title):
    start_app()
    tap_tab(2)
    ok_checkout = wait_for_text(checkout_label, contains=False) and tap(text=checkout_label)
    ok_place = ok_checkout and wait_for_text(place_label, contains=False) and tap(text=place_label)
    done = ok_place and wait_for_text(done_title)
    return ok_checkout, ok_place, done


def main():
    print(f"device={SERIAL or 'default'} apk={APK} tmp={TMP}")

    # --- install
    adb("install", "-r", APK)
    perms = shell(f"dumpsys package {PKG}")
    check("package installed", PKG in perms)
    check("no INTERNET permission", "android.permission.INTERNET" not in perms)
    check("debuggable (run-as works)", "snackorders" in shell(f"run-as {PKG} pwd", check=False))

    # --- fresh install, no seed
    shell(f"pm clear {PKG}")
    shell("logcat -c")
    start_app()
    time.sleep(2)
    crash = "FATAL EXCEPTION" in shell("logcat -d -s AndroidRuntime:E")
    check("fresh launch, no seed: app alive, no crash", app_alive() and not crash)
    conn = pull_db()
    check("fresh DB has built-in catalogue, empty cart, no orders",
          q1(conn, "SELECT COUNT(*) FROM products")[0] > 0
          and q1(conn, "SELECT COUNT(*) FROM cart_items")[0] == 0
          and q1(conn, "SELECT COUNT(*) FROM orders")[0] == 0)
    force_stop()

    # --- example seed
    seed = base_seed()
    push_seed(seed)
    start_app()
    files = seed_file_state()
    check("seed.json consumed after launch", "seed.json" not in files and "seed.rejected.json" not in files, str(files))
    conn = pull_db()
    products = q1(conn, "SELECT COUNT(*) FROM products")[0]
    cart = conn.execute("SELECT product_id, quantity FROM cart_items ORDER BY product_id").fetchall()
    orders = conn.execute("SELECT id, status, subtotal_cents, shipping_cents, total_cents FROM orders ORDER BY id").fetchall()
    check("catalogue matches seed", products == len(seed["products"]), f"{products} products")
    check("cart matches seed", cart == [(4, 2), (6, 1)], str(cart))
    check("orders match seed with computed totals",
          orders == [(1001, "DELIVERED", 1346, 369, 1715), (1002, "SHIPPED", 1098, 369, 1467)], str(orders))
    check("settings match seed",
          dict(conn.execute("SELECT key, value FROM settings")).get("variant") == "A")
    root = ui_dump()
    check("home screen shows seeded product", find_node(root, text="Cupcake") is not None, str(ui_texts(root))[:200])
    tap_tab(3)
    wait_for_text("Order #1002", contains=False)
    root = ui_dump()
    check("order history lists seeded orders", find_node(root, text="Order #1002") is not None and find_node(root, text="Order #1001") is not None,
          str(ui_texts(root)))
    tap(text="Order #1002")
    root = ui_dump()
    check("receipt shows order id, items and total",
          find_node(root, text="Order #1002") is not None and find_node(root, text="2 × Almonds") is not None
          and find_node(root, text="$14.67") is not None, str(ui_texts(root)))
    check("receipt has share action", find_node(root, text="Share receipt") is not None)
    tap(text="Share receipt")
    time.sleep(2)
    focus = shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    check("share sheet opens", "chooser" in focus.lower() or "resolver" in focus.lower() or "sharesheet" in focus.lower(), focus.strip())
    shell("input keyevent BACK")
    time.sleep(1)
    force_stop()

    # --- provider modes
    push_seed(base_seed(provider_mode="full"))
    rows = provider_rows()
    check("provider full: both orders with totals", [r["_id"] for r in rows] == ["1002", "1001"] and rows[0]["total_cents"] == "1467", str(rows))
    items = provider_rows(PROVIDER + "/1002/items")
    check("provider items for an order", len(items) == 1 and items[0]["product_name"] == "Almonds", str(items))
    push_seed(base_seed(provider_mode="stale"))
    rows = provider_rows()
    check("provider stale: newest order omitted", [r["_id"] for r in rows] == ["1001"], str(rows))
    check("provider stale: newest order items empty", provider_rows(PROVIDER + "/1002/items") == [])
    push_seed(base_seed(provider_mode="partial"))
    rows = provider_rows()
    check("provider partial: totals NULL", len(rows) == 2 and all(r["total_cents"] == "NULL" for r in rows), str(rows))

    # --- place an order through the UI, variant A
    force_stop()
    push_seed(base_seed(variant="A"))
    ok_checkout, ok_place, done = place_order_via_ui("Checkout", "Place order", "Order placed")
    check("variant A: Checkout → Place order → confirmation", ok_checkout and ok_place and done)
    conn = pull_db()
    new = q1(conn, "SELECT id, status, total_cents FROM orders ORDER BY id DESC LIMIT 1")
    check("variant A: new order row with correct total (2×499 + 299 + 369)", new is not None and new[0] == 1003 and new[1] == "PLACED" and new[2] == 1666, str(new))
    check("variant A: cart emptied", q1(conn, "SELECT COUNT(*) FROM cart_items")[0] == 0)
    root = ui_dump()
    check("confirmation screen shows order number", find_node(root, text="Order #1003") is not None, str(ui_texts(root)))
    tap(text="View receipt")
    check("receipt reachable from confirmation", wait_for_text("Receipt", contains=False))
    force_stop()

    # --- variant B
    push_seed(base_seed(variant="B"))
    start_app()
    root = ui_dump()
    check("variant B: list layout shows collection heading and prices", find_node(root, text="Bakery") is not None and find_node(root, text="$4.99") is not None,
          str(ui_texts(root))[:300])
    ok_checkout, ok_place, done = place_order_via_ui("Continue", "Submit order", "Thanks! We")
    check("variant B: Continue → Submit order → confirmation", ok_checkout and ok_place and done)
    conn = pull_db()
    new = q1(conn, "SELECT id, total_cents FROM orders ORDER BY id DESC LIMIT 1")
    check("variant B: new order row with correct total", new is not None and new[0] == 1003 and new[1] == 1666, str(new))
    force_stop()

    # --- interruptions
    shell(f"pm revoke {PKG} android.permission.POST_NOTIFICATIONS", check=False)
    push_seed(base_seed(interruptions={"notification_prompt": True, "promo_dialog": True}))
    start_app()
    root = ui_dump()
    texts = " ".join(ui_texts(root))
    check("notification permission prompt shown", "notification" in texts.lower(), texts[:200])
    tap(text="Allow")
    wait_for_text("Seasonal snack boxes", contains=False)
    root = ui_dump()
    check("promo dialog shown", find_node(root, text="Seasonal snack boxes") is not None, str(ui_texts(root))[:200])
    tap(text="Not now")
    time.sleep(1)
    root = ui_dump()
    check("promo dialog dismissed", find_node(root, text="Seasonal snack boxes") is None)
    conn = pull_db()
    settings = dict(conn.execute("SELECT key, value FROM settings"))
    check("interruptions recorded as done", settings.get("promo_dialog") == "done" and settings.get("notification_prompt") == "done", str(settings))
    force_stop()
    push_seed(base_seed(interruptions={"notification_prompt": False, "promo_dialog": False}))
    start_app()
    root = ui_dump()
    check("no interruptions when switched off", find_node(root, text="Seasonal snack boxes") is None and "notification" not in " ".join(ui_texts(root)).lower())
    force_stop()

    # --- malformed seed
    before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    path = os.path.join(TMP, "bad.json")
    with open(path, "w") as f:
        f.write('{"products": [{"id": 1}]}')
    adb("push", path, "/data/local/tmp/seed.json")
    shell(f"run-as {PKG} cp /data/local/tmp/seed.json files/seed.json")
    start_app()
    files = seed_file_state()
    conn = pull_db()
    check("malformed seed rejected and renamed, DB untouched",
          "seed.rejected.json" in files and "seed.json" not in files and conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before, str(files))
    shell(f"run-as {PKG} rm files/seed.rejected.json", check=False)

    # --- no cancellation anywhere
    strings = open(os.path.join(ROOT, "app", "src", "main", "res", "values", "strings.xml")).read().lower()
    check("no cancel/refund wording in app strings", "cancel" not in strings and "refund" not in strings)

    force_stop()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
