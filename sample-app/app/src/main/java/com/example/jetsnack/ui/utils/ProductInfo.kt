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

package com.example.jetsnack.ui.utils

import androidx.compose.runtime.Composable
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import com.example.jetsnack.R
import com.example.jetsnack.model.Snack

/** Dietary labels for the product's tags, in catalogue order ("Nut-free · Vegan"), or null without tags. */
@Composable
fun dietaryLabel(snack: Snack): String? {
    if (snack.tags.isEmpty()) return null
    return snack.tags.map { tag ->
        when (tag) {
            "nut-free" -> stringResource(R.string.tag_nut_free)
            "contains-nuts" -> stringResource(R.string.tag_contains_nuts)
            "vegan" -> stringResource(R.string.tag_vegan)
            "gluten-free" -> stringResource(R.string.tag_gluten_free)
            "dairy-free" -> stringResource(R.string.tag_dairy_free)
            else -> tag.replace('-', ' ').replaceFirstChar { it.uppercase() }
        }
    }.joinToString(" · ")
}

@Composable
fun servingsLabel(snack: Snack): String? = snack.servingSize?.let {
    pluralStringResource(if (isVariantB()) R.plurals.serves_b else R.plurals.serves, it, it)
}

@Composable
fun deliveryLabel(snack: Snack): String? = snack.deliveryDays?.let {
    pluralStringResource(if (isVariantB()) R.plurals.delivery_days_b else R.plurals.delivery_days, it, it)
}

/** One line for product lists: dietary labels, servings and delivery time, or null when none is known. */
@Composable
fun productInfoLine(snack: Snack): String? =
    listOfNotNull(dietaryLabel(snack), servingsLabel(snack), deliveryLabel(snack)).joinToString(" · ").ifEmpty { null }
