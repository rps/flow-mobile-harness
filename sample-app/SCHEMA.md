# Database schema

Package: `com.labs.snackorders`
Database file on the device: `/data/data/com.labs.snackorders/databases/snackorders.db`

Plain SQLite (no Room). Journal mode is `TRUNCATE` and write-ahead logging is off, so the
single `snackorders.db` file is complete on its own; there is no `-wal` or `-shm` file to pull.

Pull it with:

```sh
adb -s <serial> shell run-as com.labs.snackorders cat databases/snackorders.db > snackorders.db
sqlite3 snackorders.db 'SELECT * FROM orders;'
```

Conventions:

- Money is in integer **cents** (`299` = $2.99). The UI formats with the device locale.
- Timestamps are **epoch milliseconds, UTC**. The UI shows them in the device time zone.
- Foreign keys are enforced.

## products

The catalogue shown on the Home and Search tabs.

| Column        | Type    | Notes                                                                          |
| ------------- | ------- | ------------------------------------------------------------------------------ |
| `id`          | INTEGER | Primary key. From the seed, or 1..N for the built-in catalogue.                |
| `name`        | TEXT    | Not null.                                                                      |
| `tagline`     | TEXT    | Not null, default `''`.                                                        |
| `price_cents` | INTEGER | Not null. Unit price.                                                          |
| `image`       | TEXT    | Not null. Name of a bundled drawable (see SEED_FORMAT.md); `''` = placeholder. |
| `collection`  | TEXT    | Not null. Section heading the product appears under on the Home tab.           |
| `position`    | INTEGER | Not null. Display order within the catalogue.                                  |
| `tags`        | TEXT    | Not null, default `''`. Comma-separated dietary tags, e.g. `nut-free,vegan`.   |
| `serving_size`  | INTEGER | Servings in one unit. NULL = not stated (nothing shown).                     |
| `delivery_days` | INTEGER | Days from ordering to delivery. NULL = not stated (nothing shown).           |

Database version 2 added `tags`, `serving_size` and `delivery_days`; a version-1 database is
migrated in place with `ALTER TABLE` on first open, so `adb install -r` over an older build keeps
its data. The built-in catalogue has none of the three.

Where they appear: the product detail screen lists "Dietary", "Pack size" ("Serves 4") and
"Delivery" ("Delivery in 1 day") above the description, and then leaves out the sample
ingredient list; search results and variant B's Home rows show one line
("Nut-free · Vegan · Serves 4 · Delivery in 1 day"; variant B words it "4 servings · Arrives in 1 day");
the Home tab's highlight cards show the dietary labels only. Known tags get fixed labels
(`nut-free` Nut-free, `contains-nuts` Contains nuts, `vegan`, `gluten-free`, `dairy-free`);
others are shown with dashes as spaces. Products are not exposed through the ContentProvider.

## cart_items

The current cart. One row per product.

| Column       | Type    | Notes                                                      |
| ------------ | ------- | ---------------------------------------------------------- |
| `product_id` | INTEGER | Primary key, references `products(id)`.                    |
| `quantity`   | INTEGER | Not null, `> 0`. Rows are deleted when quantity reaches 0. |

## orders

| Column           | Type    | Notes                                                                                                   |
| ---------------- | ------- | ------------------------------------------------------------------------------------------------------- |
| `id`             | INTEGER | Primary key, autoincrement. Shown in the UI as "Order #<id>". Seeded ids are kept.                      |
| `placed_at`      | INTEGER | Not null. Epoch milliseconds UTC. New orders use the device clock at the moment of placing.             |
| `status`         | TEXT    | Not null. New orders are `PLACED`. Seeded orders carry whatever the seed says (`DELIVERED` by default). |
| `subtotal_cents` | INTEGER | Not null. `SUM(quantity * unit_price_cents)` over the order's items.                                    |
| `shipping_cents` | INTEGER | Not null. Shipping charged for this order.                                                              |
| `total_cents`    | INTEGER | Not null. `subtotal_cents + shipping_cents`.                                                            |

Orders are never updated or deleted by the app. There is no cancellation or refund.

## order_items

| Column             | Type    | Notes                                                                         |
| ------------------ | ------- | ----------------------------------------------------------------------------- |
| `id`               | INTEGER | Primary key, autoincrement.                                                   |
| `order_id`         | INTEGER | Not null, references `orders(id)`.                                            |
| `product_id`       | INTEGER | Not null. Not a foreign key: products may disappear from the catalogue later. |
| `product_name`     | TEXT    | Not null. Name at the time of ordering.                                       |
| `quantity`         | INTEGER | Not null, `> 0`.                                                              |
| `unit_price_cents` | INTEGER | Not null. Price at the time of ordering.                                      |

## settings

Key/value pairs controlled by the seed.

| Key                   | Values                               | Meaning                                                                        |
| --------------------- | ------------------------------------ | ------------------------------------------------------------------------------ |
| `variant`             | `A` (default), `B`                   | UI layout and wording variant.                                                 |
| `provider_mode`       | `full` (default), `stale`, `partial` | What the orders ContentProvider returns.                                       |
| `shipping_cents`      | integer, default `369`               | Shipping added to new orders.                                                  |
| `notification_prompt` | `off` (default), `pending`, `done`   | `pending` = ask for notification permission on next launch; `done` once asked. |
| `promo_dialog`        | `off` (default), `pending`, `done`   | `pending` = show the promotional dialog; `done` once dismissed.                |

## Useful queries

```sql
-- newest order with its total
SELECT id, placed_at, status, total_cents FROM orders ORDER BY placed_at DESC, id DESC LIMIT 1;

-- line items of an order
SELECT product_name, quantity, unit_price_cents FROM order_items WHERE order_id = ? ORDER BY id;

-- current cart with prices
SELECT p.name, c.quantity, p.price_cents FROM cart_items c JOIN products p ON p.id = c.product_id;

-- number of orders placed after a given time
SELECT COUNT(*) FROM orders WHERE placed_at > ?;
```

## Orders ContentProvider (read-only)

Authority `com.labs.snackorders.provider`, exported, no permission required.

| URI                                                         | Columns                                                                                                      |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `content://com.labs.snackorders.provider/orders`            | `_id`, `placed_at`, `status`, `item_count`, `subtotal_cents`, `shipping_cents`, `total_cents` (newest first) |
| `content://com.labs.snackorders.provider/orders/<id>/items` | `order_id`, `product_id`, `product_name`, `quantity`, `unit_price_cents`                                     |

`provider_mode` changes the result:

- `full`: everything in the `orders` table.
- `stale`: the newest order (highest `placed_at`, then `id`) is left out, and its items URI returns no rows.
- `partial`: all orders, but `subtotal_cents`, `shipping_cents` and `total_cents` are NULL.

Insert, update and delete throw. Example:

```sh
adb -s <serial> shell content query --uri content://com.labs.snackorders.provider/orders
adb -s <serial> shell content query --uri content://com.labs.snackorders.provider/orders/1001/items
```

Querying the provider also applies a waiting `files/seed.json`, so a seed can be checked without launching the UI.
