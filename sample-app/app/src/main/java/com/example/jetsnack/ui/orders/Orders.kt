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

package com.example.jetsnack.ui.orders

import android.content.Context
import android.content.Intent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.example.jetsnack.R
import com.example.jetsnack.data.Order
import com.example.jetsnack.data.Store
import com.example.jetsnack.ui.checkout.LineItemRow
import com.example.jetsnack.ui.checkout.ScreenTitleBar
import com.example.jetsnack.ui.checkout.SectionHeader
import com.example.jetsnack.ui.components.JetsnackButton
import com.example.jetsnack.ui.components.JetsnackDivider
import com.example.jetsnack.ui.components.JetsnackSurface
import com.example.jetsnack.ui.home.cart.SummaryItem
import com.example.jetsnack.ui.theme.JetsnackTheme
import com.example.jetsnack.ui.utils.formatPrice
import com.example.jetsnack.ui.utils.isVariantB
import com.example.jetsnack.ui.utils.variantString
import java.text.DateFormat
import java.util.Date
import java.util.Locale

fun formatOrderDate(placedAt: Long): String = DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(placedAt))

fun formatOrderStatus(status: String): String = status.lowercase(Locale.getDefault()).replaceFirstChar { it.titlecase(Locale.getDefault()) }

/**
 * List of past orders, newest first.
 */
@Composable
fun OrderHistory(onOrderSelected: (Long) -> Unit, modifier: Modifier = Modifier) {
    val orders by Store.orders.collectAsStateWithLifecycle()
    val variantB = isVariantB()
    JetsnackSurface(modifier = modifier.fillMaxSize()) {
        LazyColumn(Modifier.statusBarsPadding()) {
            item(key = "title") {
                SectionHeader(variantString(R.string.orders_title, R.string.orders_title_b))
            }
            if (orders.isEmpty()) {
                item(key = "empty") {
                    Text(
                        text = stringResource(R.string.orders_empty),
                        style = MaterialTheme.typography.bodyLarge,
                        color = JetsnackTheme.colors.textHelp,
                        modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp),
                    )
                }
            }
            items(orders, key = { it.id }) { order ->
                OrderRow(order = order, variantB = variantB, onClick = { onOrderSelected(order.id) })
            }
        }
    }
}

@Composable
private fun OrderRow(order: Order, variantB: Boolean, onClick: () -> Unit) {
    val itemCount = order.items.sumOf { it.quantity }
    val number = stringResource(R.string.order_number, order.id)
    val date = formatOrderDate(order.placedAt)
    val count = pluralStringResource(R.plurals.order_item_count, itemCount, itemCount)
    val status = formatOrderStatus(order.status)
    Column(
        Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick),
    ) {
        Row(Modifier.padding(horizontal = 24.dp, vertical = 12.dp)) {
            Column(Modifier.weight(1f)) {
                Text(
                    text = if (variantB) date else number,
                    style = MaterialTheme.typography.titleMedium,
                    color = JetsnackTheme.colors.textSecondary,
                )
                Text(
                    text = if (variantB) "$number · $status" else date,
                    style = MaterialTheme.typography.bodyMedium,
                    color = JetsnackTheme.colors.textHelp,
                )
                Text(
                    text = if (variantB) count else "$count · $status",
                    style = MaterialTheme.typography.bodyMedium,
                    color = JetsnackTheme.colors.textHelp,
                )
            }
            Text(
                text = formatPrice(order.total),
                style = MaterialTheme.typography.titleMedium,
                color = JetsnackTheme.colors.textPrimary,
            )
        }
        JetsnackDivider()
    }
}

/**
 * Details of one order, with the option to send them to another app as text.
 */
@Composable
fun Receipt(orderId: Long, upPress: () -> Unit, modifier: Modifier = Modifier) {
    val orders by Store.orders.collectAsStateWithLifecycle()
    val order = orders.find { it.id == orderId }
    val context = LocalContext.current
    val variantB = isVariantB()
    JetsnackSurface(modifier = modifier.fillMaxSize()) {
        Column(
            Modifier
                .statusBarsPadding()
                .navigationBarsPadding(),
        ) {
            ScreenTitleBar(title = variantString(R.string.receipt_title, R.string.receipt_title_b), upPress = upPress)
            if (order == null) {
                Text(
                    text = stringResource(R.string.receipt_not_found),
                    style = MaterialTheme.typography.bodyLarge,
                    modifier = Modifier.padding(24.dp),
                )
                return@Column
            }
            Column(
                Modifier
                    .weight(1f)
                    .verticalScroll(rememberScrollState()),
            ) {
                val heading: @Composable () -> Unit = {
                    SectionHeader(stringResource(R.string.order_number, order.id))
                    Text(
                        text = stringResource(R.string.receipt_placed, formatOrderDate(order.placedAt)),
                        style = MaterialTheme.typography.bodyLarge,
                        modifier = Modifier.padding(horizontal = 24.dp),
                    )
                    Text(
                        text = stringResource(R.string.receipt_status, formatOrderStatus(order.status)),
                        style = MaterialTheme.typography.bodyLarge,
                        modifier = Modifier.padding(horizontal = 24.dp),
                    )
                    Spacer(Modifier.height(8.dp))
                }
                val items: @Composable () -> Unit = {
                    order.items.forEach { item ->
                        LineItemRow(name = item.name, quantity = item.quantity, unitPrice = item.unitPrice)
                    }
                }
                val summary: @Composable () -> Unit = {
                    SummaryItem(subtotal = order.subtotal, shippingCosts = order.shipping)
                }
                heading()
                if (variantB) {
                    summary()
                    items()
                } else {
                    items()
                    summary()
                }
            }
            JetsnackDivider()
            JetsnackButton(
                onClick = { shareReceipt(context, order) },
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 24.dp, vertical = 12.dp),
            ) {
                Text(
                    text = variantString(R.string.receipt_share, R.string.receipt_share_b),
                    modifier = Modifier.fillMaxWidth(),
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                )
            }
        }
    }
}

fun receiptText(context: Context, order: Order): String = buildString {
    appendLine("${context.getString(R.string.app_name)} ${context.getString(R.string.receipt_title).lowercase(Locale.getDefault())}")
    appendLine(context.getString(R.string.order_number, order.id))
    appendLine(context.getString(R.string.receipt_placed, formatOrderDate(order.placedAt)))
    appendLine(context.getString(R.string.receipt_status, formatOrderStatus(order.status)))
    appendLine()
    order.items.forEach { item ->
        appendLine("${item.quantity} x ${item.name} @ ${formatPrice(item.unitPrice)} = ${formatPrice(item.unitPrice * item.quantity)}")
    }
    appendLine()
    appendLine("${context.getString(R.string.cart_subtotal_label)}: ${formatPrice(order.subtotal)}")
    appendLine("${context.getString(R.string.cart_shipping_label)}: ${formatPrice(order.shipping)}")
    append("${context.getString(R.string.cart_total_label)}: ${formatPrice(order.total)}")
}

private fun shareReceipt(context: Context, order: Order) {
    val send = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_SUBJECT, context.getString(R.string.order_number, order.id))
        putExtra(Intent.EXTRA_TEXT, receiptText(context, order))
    }
    context.startActivity(Intent.createChooser(send, null))
}
