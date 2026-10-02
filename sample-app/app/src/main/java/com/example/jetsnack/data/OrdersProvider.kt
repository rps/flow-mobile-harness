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

import android.content.ContentProvider
import android.content.ContentValues
import android.content.UriMatcher
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri

/**
 * Read-only view of the order history for other apps.
 *
 * `content://com.labs.snackorders.provider/orders` lists orders, newest first.
 * `content://com.labs.snackorders.provider/orders/<id>/items` lists one order's line items.
 *
 * The `provider_mode` setting controls how complete the data is: `full` returns everything,
 * `stale` leaves out the newest order, `partial` returns orders without their amounts.
 */
class OrdersProvider : ContentProvider() {

    override fun onCreate(): Boolean = true

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor? {
        val context = context ?: return null
        Store.init(context)
        val mode = Store.settings.value.providerMode
        val all = Store.readOrders()
        val visible = if (mode == "stale") all.drop(1) else all
        return when (matcher.match(uri)) {
            ORDERS -> MatrixCursor(ORDER_COLUMNS).apply {
                visible.forEach { order ->
                    val partial = mode == "partial"
                    addRow(
                        arrayOf<Any?>(
                            order.id,
                            order.placedAt,
                            order.status,
                            order.items.sumOf { it.quantity },
                            if (partial) null else order.subtotal,
                            if (partial) null else order.shipping,
                            if (partial) null else order.total,
                        ),
                    )
                }
            }

            ORDER_ITEMS -> MatrixCursor(ITEM_COLUMNS).apply {
                val orderId = uri.pathSegments[1].toLongOrNull()
                visible.find { it.id == orderId }?.items?.forEach { item ->
                    addRow(arrayOf<Any?>(orderId, item.productId, item.name, item.quantity, item.unitPrice))
                }
            }

            else -> null
        }
    }

    override fun getType(uri: Uri): String? = when (matcher.match(uri)) {
        ORDERS -> "vnd.android.cursor.dir/vnd.$AUTHORITY.order"
        ORDER_ITEMS -> "vnd.android.cursor.dir/vnd.$AUTHORITY.order_item"
        else -> null
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri? = throw UnsupportedOperationException("Read-only")

    override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int =
        throw UnsupportedOperationException("Read-only")

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int =
        throw UnsupportedOperationException("Read-only")

    companion object {
        const val AUTHORITY = "com.labs.snackorders.provider"
        private const val ORDERS = 1
        private const val ORDER_ITEMS = 2
        private val ORDER_COLUMNS =
            arrayOf("_id", "placed_at", "status", "item_count", "subtotal_cents", "shipping_cents", "total_cents")
        private val ITEM_COLUMNS = arrayOf("order_id", "product_id", "product_name", "quantity", "unit_price_cents")
        private val matcher = UriMatcher(UriMatcher.NO_MATCH).apply {
            addURI(AUTHORITY, "orders", ORDERS)
            addURI(AUTHORITY, "orders/#/items", ORDER_ITEMS)
        }
    }
}
