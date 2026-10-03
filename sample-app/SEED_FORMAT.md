# Seed file format

Path on the device: `/data/data/com.labs.snackorders/files/seed.json`

Push it with:

```sh
adb -s <serial> shell am force-stop com.labs.snackorders
adb -s <serial> push examples/seed.json /data/local/tmp/seed.json
adb -s <serial> shell run-as com.labs.snackorders cp /data/local/tmp/seed.json files/seed.json
adb -s <serial> shell rm /data/local/tmp/seed.json
adb -s <serial> shell am start -n com.labs.snackorders/com.example.jetsnack.ui.MainActivity
```

(`files/` may not exist before the first launch; `run-as ... mkdir -p files` first if needed.)

## When it is applied

The app checks for the file every time the main activity is created and every time the
orders ContentProvider is queried. If the file exists, the whole database is wiped and
rebuilt from it inside one transaction, and the file is then **deleted**. If the file
cannot be parsed or fails validation, the database is left untouched, the file is renamed
`files/seed.rejected.json`, and the reason is logged under tag `SnackOrders`.

Force-stop the app before pushing so the next launch is a cold start with the new data.

Without a seed, a fresh install has the built-in Jetsnack catalogue (ids 1–N in the order
they appear in `Snack.kt`), an empty cart and no orders.

## Fields

All fields are optional except where noted. Money is in integer cents, times in epoch milliseconds UTC.

```jsonc
{
  "variant": "A", // "A" (default) or "B": UI layout and wording
  "provider_mode": "full", // "full" (default), "stale" or "partial": see SCHEMA.md
  "shipping_cents": 369, // shipping added to new orders (default 369)
  "interruptions": {
    "notification_prompt": false, // ask for notification permission on next launch
    "promo_dialog": false, // show a dismissible promotional dialog on next launch
  },
  "products": [
    // omit the key entirely to keep the built-in catalogue
    {
      "id": 1, // required, unique
      "name": "Cupcake", // required
      "tagline": "A tag line", // default ""
      "price_cents": 299, // required
      "image": "cupcake", // bundled drawable name, default placeholder
      "collection": "Android's picks", // Home tab section, default "Snacks"
      "tags": ["nut-free", "vegan"], // dietary tags, lowercase words joined by "-"; default none
      "serving_size": 4, // servings in one unit, > 0; omit the key for "not stated" (null is rejected)
      "delivery_days": 1, // days from ordering to delivery, > 0; omit the key for "not stated" (null is rejected)
    },
  ],
  "cart": [
    { "product_id": 1, "quantity": 2 }, // product_id must exist; quantity > 0
  ],
  "orders": [
    {
      "id": 1001, // optional; otherwise autoincrement
      "placed_at": 1759000000000, // required
      "status": "DELIVERED", // default "DELIVERED"; the app writes "PLACED" for new orders
      "shipping_cents": 369, // default: top-level shipping_cents
      "items": [
        // required, at least one
        {
          "product_id": 1, // required
          "quantity": 2, // required, > 0
          "unit_price_cents": 299, // default: the product's current price
          "name": "Cupcake", // default: the product's current name
        },
      ],
    },
  ],
}
```

Order totals are always computed by the app (`subtotal = Σ quantity × unit_price_cents`,
`total = subtotal + shipping_cents`); they cannot be set directly, so the database stays
internally consistent. An order item may reference a product id that is not in the catalogue
as long as it gives both `name` and `unit_price_cents`.

Available image names: `almonds`, `apple_chips`, `apple_juice`, `apple_pie`, `apple_sauce`,
`apples`, `cheese`, `chips`, `cupcake`, `donut`, `eclair`, `froyo`, `gingerbread`, `grapes`,
`honeycomb`, `ice_cream_sandwich`, `jelly_bean`, `kitkat`, `kiwi`, `lollipop`, `mango`,
`marshmallow`, `nougat`, `nuts`, `oreo`, `pie`, `popcorn`, `pretzels`, `smoothies`, `placeholder`.

The example seed is in `examples/seed.json`.
