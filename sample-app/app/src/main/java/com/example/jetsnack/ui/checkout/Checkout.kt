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

package com.example.jetsnack.ui.checkout

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.wrapContentHeight
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.example.jetsnack.R
import com.example.jetsnack.data.Store
import com.example.jetsnack.ui.components.JetsnackButton
import com.example.jetsnack.ui.components.JetsnackDivider
import com.example.jetsnack.ui.components.JetsnackSurface
import com.example.jetsnack.ui.home.cart.SummaryItem
import com.example.jetsnack.ui.theme.JetsnackTheme
import com.example.jetsnack.ui.utils.formatPrice
import com.example.jetsnack.ui.utils.isVariantB
import com.example.jetsnack.ui.utils.variantString

/**
 * Last step before an order is created: shows what is about to be ordered and the amount.
 */
@Composable
fun ReviewOrder(upPress: () -> Unit, onOrderPlaced: (Long) -> Unit, modifier: Modifier = Modifier) {
    val orderLines by Store.cart.collectAsStateWithLifecycle()
    val settings by Store.settings.collectAsStateWithLifecycle()
    val variantB = isVariantB()
    val subtotal = orderLines.sumOf { it.snack.price * it.count }
    JetsnackSurface(modifier = modifier.fillMaxSize()) {
        Column(
            Modifier
                .statusBarsPadding()
                .navigationBarsPadding(),
        ) {
            ScreenTitleBar(title = variantString(R.string.review_title, R.string.review_title_b), upPress = upPress)
            Column(
                Modifier
                    .weight(1f)
                    .verticalScroll(rememberScrollState()),
            ) {
                if (orderLines.isEmpty()) {
                    Text(
                        text = stringResource(R.string.cart_empty),
                        style = MaterialTheme.typography.bodyLarge,
                        modifier = Modifier.padding(24.dp),
                    )
                } else {
                    val items: @Composable () -> Unit = {
                        SectionHeader(variantString(R.string.review_items_header, R.string.review_items_header_b))
                        orderLines.forEach { line ->
                            LineItemRow(
                                name = line.snack.name,
                                quantity = line.count,
                                unitPrice = line.snack.price,
                            )
                        }
                    }
                    val summary: @Composable () -> Unit = {
                        SummaryItem(subtotal = subtotal, shippingCosts = settings.shipping)
                    }
                    if (variantB) {
                        summary()
                        items()
                    } else {
                        items()
                        Spacer(Modifier.height(16.dp))
                        summary()
                    }
                }
            }
            JetsnackDivider()
            JetsnackButton(
                onClick = { Store.placeOrder()?.let(onOrderPlaced) },
                enabled = orderLines.isNotEmpty(),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 24.dp, vertical = 12.dp),
            ) {
                Text(
                    text = variantString(R.string.review_place_order, R.string.review_place_order_b),
                    modifier = Modifier.fillMaxWidth(),
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                )
            }
        }
    }
}

/**
 * Shown once an order has been created.
 */
@Composable
fun OrderConfirmation(orderId: Long, onViewReceipt: (Long) -> Unit, onDone: () -> Unit, modifier: Modifier = Modifier) {
    val orders by Store.orders.collectAsStateWithLifecycle()
    val order = orders.find { it.id == orderId }
    JetsnackSurface(modifier = modifier.fillMaxSize()) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
            modifier = Modifier
                .statusBarsPadding()
                .navigationBarsPadding()
                .padding(24.dp),
        ) {
            Icon(
                painter = painterResource(R.drawable.ic_check),
                tint = JetsnackTheme.colors.brand,
                contentDescription = null,
            )
            Spacer(Modifier.height(16.dp))
            Text(
                text = variantString(R.string.confirmation_title, R.string.confirmation_title_b),
                style = MaterialTheme.typography.headlineSmall,
                color = JetsnackTheme.colors.textSecondary,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                text = stringResource(R.string.order_number, orderId),
                style = MaterialTheme.typography.titleMedium,
                color = JetsnackTheme.colors.textSecondary,
            )
            if (order != null) {
                Spacer(Modifier.height(8.dp))
                Text(
                    text = stringResource(
                        if (isVariantB()) R.string.confirmation_total_b else R.string.confirmation_total,
                        formatPrice(order.total),
                    ),
                    style = MaterialTheme.typography.bodyLarge,
                    color = JetsnackTheme.colors.textHelp,
                )
            }
            Spacer(Modifier.height(32.dp))
            JetsnackButton(onClick = { onViewReceipt(orderId) }, modifier = Modifier.fillMaxWidth()) {
                Text(
                    text = variantString(R.string.confirmation_view_receipt, R.string.confirmation_view_receipt_b),
                    modifier = Modifier.fillMaxWidth(),
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                )
            }
            Spacer(Modifier.height(12.dp))
            JetsnackButton(
                onClick = onDone,
                backgroundGradient = JetsnackTheme.colors.interactiveSecondary,
                contentColor = JetsnackTheme.colors.textSecondary,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    text = variantString(R.string.confirmation_done, R.string.confirmation_done_b),
                    modifier = Modifier.fillMaxWidth(),
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                )
            }
        }
    }
}

@Composable
fun ScreenTitleBar(title: String, upPress: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = 56.dp)
            .padding(horizontal = 8.dp),
    ) {
        IconButton(onClick = upPress) {
            Icon(
                painter = painterResource(R.drawable.ic_arrow_back),
                tint = JetsnackTheme.colors.iconInteractive,
                contentDescription = stringResource(R.string.label_back),
            )
        }
        Text(
            text = title,
            style = MaterialTheme.typography.titleLarge,
            color = JetsnackTheme.colors.textSecondary,
            modifier = Modifier.padding(start = 8.dp),
        )
    }
}

@Composable
fun SectionHeader(text: String, modifier: Modifier = Modifier) {
    Text(
        text = text,
        style = MaterialTheme.typography.titleLarge,
        color = JetsnackTheme.colors.brand,
        modifier = modifier
            .padding(horizontal = 24.dp)
            .heightIn(min = 56.dp)
            .wrapContentHeight(),
    )
}

@Composable
fun LineItemRow(name: String, quantity: Int, unitPrice: Long, modifier: Modifier = Modifier) {
    Row(modifier = modifier.padding(horizontal = 24.dp, vertical = 8.dp)) {
        Column(Modifier.weight(1f)) {
            Text(
                text = stringResource(R.string.order_line, quantity, name),
                style = MaterialTheme.typography.bodyLarge,
            )
            Text(
                text = stringResource(R.string.order_line_unit_price, formatPrice(unitPrice)),
                style = MaterialTheme.typography.bodyMedium,
                color = JetsnackTheme.colors.textHelp,
            )
        }
        Text(
            text = formatPrice(unitPrice * quantity),
            style = MaterialTheme.typography.bodyLarge,
        )
    }
}
