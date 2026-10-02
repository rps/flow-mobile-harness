/*
 * Copyright 2020 The Android Open Source Project
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

package com.example.jetsnack.ui.home.cart

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import com.example.jetsnack.data.Store
import com.example.jetsnack.model.OrderLine
import com.example.jetsnack.model.SnackRepo
import com.example.jetsnack.model.SnackbarManager
import kotlinx.coroutines.flow.StateFlow

/**
 * Exposes the contents of the cart, which live in the database, and allows changes to it.
 */
class CartViewModel(
    @Suppress("unused") private val snackbarManager: SnackbarManager,
    @Suppress("UNUSED_PARAMETER") snackRepository: SnackRepo,
) : ViewModel() {

    val orderLines: StateFlow<List<OrderLine>> get() = Store.cart

    fun increaseSnackCount(snackId: Long) {
        val currentCount = orderLines.value.firstOrNull { it.snack.id == snackId }?.count ?: return
        Store.setCartQuantity(snackId, currentCount + 1)
    }

    fun decreaseSnackCount(snackId: Long) {
        val currentCount = orderLines.value.firstOrNull { it.snack.id == snackId }?.count ?: return
        // a quantity of zero removes the snack from the cart
        Store.setCartQuantity(snackId, currentCount - 1)
    }

    fun removeSnack(snackId: Long) {
        Store.setCartQuantity(snackId, 0)
    }

    /**
     * Factory for CartViewModel that takes SnackbarManager as a dependency
     */
    companion object {
        fun provideFactory(
            snackbarManager: SnackbarManager = SnackbarManager,
            snackRepository: SnackRepo = SnackRepo,
        ): ViewModelProvider.Factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T {
                return CartViewModel(snackbarManager, snackRepository) as T
            }
        }
    }
}
