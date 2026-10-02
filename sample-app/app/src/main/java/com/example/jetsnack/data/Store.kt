/*
 * Copyright 2026 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.example.jetsnack.data

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.Log
import com.example.jetsnack.R
import com.example.jetsnack.model.CollectionType
import com.example.jetsnack.model.OrderLine
import com.example.jetsnack.model.Snack
import com.example.jetsnack.model.SnackCollection
import com.example.jetsnack.model.snacks
import java.io.File
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONObject

data class OrderItem(val productId: Long, val name: String, val quantity: Int, val unitPrice: Long)

data class Order(
    val id: Long,
    val placedAt: Long,
    val status: String,
    val subtotal: Long,
    val shipping: Long,
    val total: Long,
    val items: List<OrderItem>,
)

data class Settings(
    val variant: String = "A",
    val providerMode: String = "full",
    val shipping: Long = 369,
    val notificationPrompt: String = "off",
    val promoDialog: String = "off",
)

private const val TAG = "SnackOrders"
private const val DB_NAME = "snackorders.db"
const val SEED_FILE = "seed.json"
const val SEED_REJECTED_FILE = "seed.rejected.json"

private class DbHelper(context: Context) : SQLiteOpenHelper(context, DB_NAME, null, 1) {
    init {
        setWriteAheadLoggingEnabled(false)
    }

    override fun onConfigure(db: SQLiteDatabase) {
        db.setForeignKeyConstraintsEnabled(true)
        // Keep everything in the single .db file so it can be pulled and queried as is.
        db.rawQuery("PRAGMA journal_mode=TRUNCATE", null).use { it.moveToFirst() }
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                tagline TEXT NOT NULL DEFAULT '',
                price_cents INTEGER NOT NULL,
                image TEXT NOT NULL DEFAULT '',
                collection TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL
            )""",
        )
        db.execSQL(
            """CREATE TABLE cart_items (
                product_id INTEGER PRIMARY KEY REFERENCES products(id),
                quantity INTEGER NOT NULL CHECK (quantity > 0)
            )""",
        )
        db.execSQL(
            """CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                placed_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                subtotal_cents INTEGER NOT NULL,
                shipping_cents INTEGER NOT NULL,
                total_cents INTEGER NOT NULL
            )""",
        )
        db.execSQL(
            """CREATE TABLE order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price_cents INTEGER NOT NULL
            )""",
        )
        db.execSQL("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
}

/**
 * Single access point for the app's SQLite data: catalogue, cart, orders and settings.
 * The database is small, so reads and writes are done synchronously.
 */
object Store {
    private var helper: DbHelper? = null
    private lateinit var appContext: Context

    var products: List<Snack> = snacks
        private set
    var collections: List<SnackCollection> = emptyList()
        private set

    private val _cart = MutableStateFlow<List<OrderLine>>(emptyList())
    val cart: StateFlow<List<OrderLine>> get() = _cart

    private val _orders = MutableStateFlow<List<Order>>(emptyList())
    val orders: StateFlow<List<Order>> get() = _orders

    private val _settings = MutableStateFlow(Settings())
    val settings: StateFlow<Settings> get() = _settings

    private val db: SQLiteDatabase get() = helper!!.writableDatabase

    /**
     * Opens the database, loads `files/seed.json` if one is waiting, and refreshes in-memory state.
     * Safe to call repeatedly.
     */
    @Synchronized
    fun init(context: Context) {
        val first = helper == null
        if (first) {
            appContext = context.applicationContext
            helper = DbHelper(appContext)
            if (count("products") == 0L) {
                db.runInTransaction {
                    insertDefaultCatalogue()
                    writeSettings(Settings())
                }
            }
        }
        val seeded = applySeedIfPresent()
        if (first || seeded) reload()
    }

    private fun applySeedIfPresent(): Boolean {
        val file = File(appContext.filesDir, SEED_FILE)
        if (!file.exists()) return false
        return try {
            val seed = JSONObject(file.readText())
            db.runInTransaction { loadSeed(seed) }
            file.delete()
            true
        } catch (e: Exception) {
            Log.e(TAG, "Seed rejected: ${e.message}")
            file.renameTo(File(appContext.filesDir, SEED_REJECTED_FILE))
            false
        }
    }

    private fun loadSeed(seed: JSONObject) {
        val db = db
        db.delete("order_items", null, null)
        db.delete("orders", null, null)
        db.delete("cart_items", null, null)
        db.delete("products", null, null)
        db.delete("settings", null, null)
        db.delete("sqlite_sequence", null, null)

        val variant = seed.optString("variant", "A")
        require(variant == "A" || variant == "B") { "variant must be A or B" }
        val providerMode = seed.optString("provider_mode", "full")
        require(providerMode in listOf("full", "stale", "partial")) { "provider_mode must be full, stale or partial" }
        val shipping = seed.optLong("shipping_cents", 369)
        require(shipping >= 0) { "shipping_cents must not be negative" }
        val interruptions = seed.optJSONObject("interruptions")
        writeSettings(
            Settings(
                variant = variant,
                providerMode = providerMode,
                shipping = shipping,
                notificationPrompt = if (interruptions?.optBoolean("notification_prompt") == true) "pending" else "off",
                promoDialog = if (interruptions?.optBoolean("promo_dialog") == true) "pending" else "off",
            ),
        )

        val prices = HashMap<Long, Long>()
        val names = HashMap<Long, String>()
        val productsJson = seed.optJSONArray("products")
        if (productsJson == null) {
            insertDefaultCatalogue()
            db.rawQuery("SELECT id, name, price_cents FROM products", null).use { c ->
                while (c.moveToNext()) {
                    names[c.getLong(0)] = c.getString(1)
                    prices[c.getLong(0)] = c.getLong(2)
                }
            }
        } else {
            require(productsJson.length() > 0) { "products must not be empty" }
            for (i in 0 until productsJson.length()) {
                val p = productsJson.getJSONObject(i)
                val id = p.getLong("id")
                val name = p.getString("name")
                val price = p.getLong("price_cents")
                require(price >= 0) { "product $id: price_cents must not be negative" }
                require(id !in prices) { "product $id is listed twice" }
                db.insertOrThrow(
                    "products",
                    null,
                    ContentValues().apply {
                        put("id", id)
                        put("name", name)
                        put("tagline", p.optString("tagline", ""))
                        put("price_cents", price)
                        put("image", p.optString("image", ""))
                        put("collection", p.optString("collection", ""))
                        put("position", i)
                    },
                )
                prices[id] = price
                names[id] = name
            }
        }

        val cartJson = seed.optJSONArray("cart")
        for (i in 0 until (cartJson?.length() ?: 0)) {
            val line = cartJson!!.getJSONObject(i)
            val productId = line.getLong("product_id")
            require(productId in prices) { "cart: unknown product_id $productId" }
            db.insertOrThrow(
                "cart_items",
                null,
                ContentValues().apply {
                    put("product_id", productId)
                    put("quantity", line.getInt("quantity"))
                },
            )
        }

        val ordersJson = seed.optJSONArray("orders")
        for (i in 0 until (ordersJson?.length() ?: 0)) {
            val o = ordersJson!!.getJSONObject(i)
            val items = o.getJSONArray("items")
            require(items.length() > 0) { "order ${o.opt("id")}: items must not be empty" }
            val lines = (0 until items.length()).map { j ->
                val item = items.getJSONObject(j)
                val productId = item.getLong("product_id")
                OrderItem(
                    productId = productId,
                    name = if (item.has("name")) item.getString("name") else requireNotNull(names[productId]) {
                        "order item: unknown product_id $productId needs a name"
                    },
                    quantity = item.getInt("quantity"),
                    unitPrice = if (item.has("unit_price_cents")) {
                        item.getLong("unit_price_cents")
                    } else {
                        requireNotNull(prices[productId]) { "order item: unknown product_id $productId needs unit_price_cents" }
                    },
                )
            }
            insertOrder(
                id = if (o.has("id")) o.getLong("id") else null,
                placedAt = o.getLong("placed_at"),
                status = o.optString("status", "DELIVERED"),
                shipping = o.optLong("shipping_cents", shipping),
                items = lines,
            )
        }
    }

    private fun insertDefaultCatalogue() {
        snacks.forEachIndexed { index, snack ->
            db.insertOrThrow(
                "products",
                null,
                ContentValues().apply {
                    put("id", index + 1L)
                    put("name", snack.name)
                    put("tagline", snack.tagline)
                    put("price_cents", snack.price)
                    put("image", appContext.resources.getResourceEntryName(snack.imageRes))
                    put("collection", if (index < 14) "Android's picks" else "Popular on Jetsnack")
                    put("position", index)
                },
            )
        }
    }

    private fun writeSettings(settings: Settings) {
        mapOf(
            "variant" to settings.variant,
            "provider_mode" to settings.providerMode,
            "shipping_cents" to settings.shipping.toString(),
            "notification_prompt" to settings.notificationPrompt,
            "promo_dialog" to settings.promoDialog,
        ).forEach { (key, value) ->
            db.insertWithOnConflict(
                "settings",
                null,
                ContentValues().apply {
                    put("key", key)
                    put("value", value)
                },
                SQLiteDatabase.CONFLICT_REPLACE,
            )
        }
    }

    private fun insertOrder(id: Long?, placedAt: Long, status: String, shipping: Long, items: List<OrderItem>): Long {
        val subtotal = items.sumOf { it.unitPrice * it.quantity }
        val orderId = db.insertOrThrow(
            "orders",
            null,
            ContentValues().apply {
                if (id != null) put("id", id)
                put("placed_at", placedAt)
                put("status", status)
                put("subtotal_cents", subtotal)
                put("shipping_cents", shipping)
                put("total_cents", subtotal + shipping)
            },
        )
        items.forEach { item ->
            db.insertOrThrow(
                "order_items",
                null,
                ContentValues().apply {
                    put("order_id", orderId)
                    put("product_id", item.productId)
                    put("product_name", item.name)
                    put("quantity", item.quantity)
                    put("unit_price_cents", item.unitPrice)
                },
            )
        }
        return orderId
    }

    private fun reload() {
        val grouped = LinkedHashMap<String, MutableList<Snack>>()
        val all = ArrayList<Snack>()
        db.rawQuery("SELECT id, name, tagline, price_cents, image, collection FROM products ORDER BY position, id", null).use { c ->
            while (c.moveToNext()) {
                val image = appContext.resources.getIdentifier(c.getString(4), "drawable", appContext.packageName)
                val snack = Snack(
                    id = c.getLong(0),
                    name = c.getString(1),
                    tagline = c.getString(2),
                    price = c.getLong(3),
                    imageRes = if (image != 0) image else R.drawable.placeholder,
                )
                all += snack
                grouped.getOrPut(c.getString(5).ifEmpty { "Snacks" }) { ArrayList() } += snack
            }
        }
        products = all
        collections = grouped.entries.mapIndexed { index, (name, list) ->
            SnackCollection(
                id = index + 1L,
                name = name,
                snacks = list,
                type = if (index == 0) CollectionType.Highlight else CollectionType.Normal,
            )
        }
        val values = HashMap<String, String>()
        db.rawQuery("SELECT key, value FROM settings", null).use { c ->
            while (c.moveToNext()) values[c.getString(0)] = c.getString(1)
        }
        val defaults = Settings()
        _settings.value = Settings(
            variant = values["variant"] ?: defaults.variant,
            providerMode = values["provider_mode"] ?: defaults.providerMode,
            shipping = values["shipping_cents"]?.toLongOrNull() ?: defaults.shipping,
            notificationPrompt = values["notification_prompt"] ?: defaults.notificationPrompt,
            promoDialog = values["promo_dialog"] ?: defaults.promoDialog,
        )
        reloadCart()
        _orders.value = readOrders()
    }

    private fun reloadCart() {
        val byId = products.associateBy { it.id }
        val lines = ArrayList<OrderLine>()
        db.rawQuery(
            "SELECT c.product_id, c.quantity FROM cart_items c JOIN products p ON p.id = c.product_id ORDER BY p.position, p.id",
            null,
        ).use { c ->
            while (c.moveToNext()) {
                byId[c.getLong(0)]?.let { lines += OrderLine(it, c.getInt(1)) }
            }
        }
        _cart.value = lines
    }

    /** All orders, newest first, read straight from the database. */
    fun readOrders(): List<Order> {
        val items = HashMap<Long, MutableList<OrderItem>>()
        db.rawQuery("SELECT order_id, product_id, product_name, quantity, unit_price_cents FROM order_items ORDER BY id", null).use { c ->
            while (c.moveToNext()) {
                items.getOrPut(c.getLong(0)) { ArrayList() } +=
                    OrderItem(c.getLong(1), c.getString(2), c.getInt(3), c.getLong(4))
            }
        }
        val result = ArrayList<Order>()
        db.rawQuery(
            "SELECT id, placed_at, status, subtotal_cents, shipping_cents, total_cents FROM orders ORDER BY placed_at DESC, id DESC",
            null,
        ).use { c ->
            while (c.moveToNext()) {
                result += Order(
                    id = c.getLong(0),
                    placedAt = c.getLong(1),
                    status = c.getString(2),
                    subtotal = c.getLong(3),
                    shipping = c.getLong(4),
                    total = c.getLong(5),
                    items = items[c.getLong(0)].orEmpty(),
                )
            }
        }
        return result
    }

    fun getOrder(orderId: Long): Order? = _orders.value.find { it.id == orderId }

    @Synchronized
    fun addToCart(productId: Long, quantity: Int) {
        if (quantity <= 0) return
        val current = _cart.value.find { it.snack.id == productId }?.count ?: 0
        setCartQuantity(productId, current + quantity)
    }

    @Synchronized
    fun setCartQuantity(productId: Long, quantity: Int) {
        if (quantity <= 0) {
            db.delete("cart_items", "product_id = ?", arrayOf(productId.toString()))
        } else {
            db.insertWithOnConflict(
                "cart_items",
                null,
                ContentValues().apply {
                    put("product_id", productId)
                    put("quantity", quantity)
                },
                SQLiteDatabase.CONFLICT_REPLACE,
            )
        }
        reloadCart()
    }

    /** Turns the current cart into an order and empties the cart. Returns the new order id, or null if the cart is empty. */
    @Synchronized
    fun placeOrder(): Long? {
        val lines = _cart.value
        if (lines.isEmpty()) return null
        var orderId = 0L
        db.runInTransaction {
            orderId = insertOrder(
                id = null,
                placedAt = System.currentTimeMillis(),
                status = "PLACED",
                shipping = _settings.value.shipping,
                items = lines.map { OrderItem(it.snack.id, it.snack.name, it.count, it.snack.price) },
            )
            db.delete("cart_items", null, null)
        }
        reloadCart()
        _orders.value = readOrders()
        return orderId
    }

    @Synchronized
    fun markNotificationPromptDone() = updateSetting("notification_prompt", "done")

    @Synchronized
    fun markPromoDialogDone() = updateSetting("promo_dialog", "done")

    private fun updateSetting(key: String, value: String) {
        db.update("settings", ContentValues().apply { put("value", value) }, "key = ?", arrayOf(key))
        _settings.value = when (key) {
            "notification_prompt" -> _settings.value.copy(notificationPrompt = value)
            else -> _settings.value.copy(promoDialog = value)
        }
    }

    private fun count(table: String): Long = db.rawQuery("SELECT COUNT(*) FROM $table", null).use {
        it.moveToFirst()
        it.getLong(0)
    }

    private inline fun SQLiteDatabase.runInTransaction(block: () -> Unit) {
        beginTransaction()
        try {
            block()
            setTransactionSuccessful()
        } finally {
            endTransaction()
        }
    }
}
