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

@file:OptIn(
    ExperimentalSharedTransitionApi::class,
)

package com.example.jetsnack.ui

import androidx.compose.animation.AnimatedVisibilityScope
import androidx.compose.animation.ExperimentalSharedTransitionApi
import androidx.compose.animation.SharedTransitionLayout
import androidx.compose.animation.SharedTransitionScope
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.tooling.preview.Preview
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavBackStackEntry
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.navArgument
import com.example.jetsnack.R
import com.example.jetsnack.data.Store
import com.example.jetsnack.ui.checkout.OrderConfirmation
import com.example.jetsnack.ui.checkout.ReviewOrder
import com.example.jetsnack.ui.components.JetsnackScaffold
import com.example.jetsnack.ui.components.JetsnackSnackbar
import com.example.jetsnack.ui.components.rememberJetsnackScaffoldState
import com.example.jetsnack.ui.home.HomeSections
import com.example.jetsnack.ui.home.JetsnackBottomBar
import com.example.jetsnack.ui.home.addHomeGraph
import com.example.jetsnack.ui.home.composableWithCompositionLocal
import com.example.jetsnack.ui.navigation.MainDestinations
import com.example.jetsnack.ui.navigation.rememberJetsnackNavController
import com.example.jetsnack.ui.orders.Receipt
import com.example.jetsnack.ui.snackdetail.SnackDetail
import com.example.jetsnack.ui.snackdetail.nonSpatialExpressiveSpring
import com.example.jetsnack.ui.snackdetail.spatialExpressiveSpring
import com.example.jetsnack.ui.theme.JetsnackTheme

@Preview
@Composable
fun JetsnackApp() {
    JetsnackTheme {
        val jetsnackNavController = rememberJetsnackNavController()
        SharedTransitionLayout {
            CompositionLocalProvider(
                LocalSharedTransitionScope provides this,
            ) {
                NavHost(
                    navController = jetsnackNavController.navController,
                    startDestination = MainDestinations.HOME_ROUTE,
                ) {
                    composableWithCompositionLocal(
                        route = MainDestinations.HOME_ROUTE,
                    ) { backStackEntry ->
                        MainContainer(
                            onSnackSelected = jetsnackNavController::navigateToSnackDetail,
                            onCheckout = jetsnackNavController::navigateToReview,
                            onOrderSelected = jetsnackNavController::navigateToReceipt,
                        )
                    }

                    composableWithCompositionLocal(
                        "${MainDestinations.SNACK_DETAIL_ROUTE}/" +
                            "{${MainDestinations.SNACK_ID_KEY}}" +
                            "?origin={${MainDestinations.ORIGIN}}",
                        arguments = listOf(
                            navArgument(MainDestinations.SNACK_ID_KEY) {
                                type = NavType.LongType
                            },
                        ),

                    ) { backStackEntry ->
                        val arguments = requireNotNull(backStackEntry.arguments)
                        val snackId = arguments.getLong(MainDestinations.SNACK_ID_KEY)
                        val origin = arguments.getString(MainDestinations.ORIGIN)
                        SnackDetail(
                            snackId,
                            origin = origin ?: "",
                            upPress = jetsnackNavController::upPress,
                        )
                    }

                    composableWithCompositionLocal(MainDestinations.REVIEW_ROUTE) {
                        ReviewOrder(
                            upPress = jetsnackNavController::upPress,
                            onOrderPlaced = jetsnackNavController::navigateToConfirmation,
                        )
                    }

                    composableWithCompositionLocal(
                        "${MainDestinations.CONFIRMATION_ROUTE}/{${MainDestinations.ORDER_ID_KEY}}",
                        arguments = listOf(navArgument(MainDestinations.ORDER_ID_KEY) { type = NavType.LongType }),
                    ) { backStackEntry ->
                        OrderConfirmation(
                            orderId = requireNotNull(backStackEntry.arguments).getLong(MainDestinations.ORDER_ID_KEY),
                            onViewReceipt = jetsnackNavController::navigateToReceipt,
                            onDone = jetsnackNavController::returnHome,
                        )
                    }

                    composableWithCompositionLocal(
                        "${MainDestinations.RECEIPT_ROUTE}/{${MainDestinations.ORDER_ID_KEY}}",
                        arguments = listOf(navArgument(MainDestinations.ORDER_ID_KEY) { type = NavType.LongType }),
                    ) { backStackEntry ->
                        Receipt(
                            orderId = requireNotNull(backStackEntry.arguments).getLong(MainDestinations.ORDER_ID_KEY),
                            upPress = jetsnackNavController::upPress,
                        )
                    }
                }
            }
        }
        PromoDialog()
    }
}

@Composable
private fun PromoDialog() {
    val settings by Store.settings.collectAsStateWithLifecycle()
    if (settings.promoDialog == "pending") {
        AlertDialog(
            onDismissRequest = Store::markPromoDialogDone,
            title = { Text(stringResource(R.string.promo_title)) },
            text = { Text(stringResource(R.string.promo_text)) },
            confirmButton = {
                TextButton(onClick = Store::markPromoDialogDone) { Text(stringResource(R.string.promo_ok)) }
            },
            dismissButton = {
                TextButton(onClick = Store::markPromoDialogDone) { Text(stringResource(R.string.promo_dismiss)) }
            },
        )
    }
}

@Composable
fun MainContainer(
    modifier: Modifier = Modifier,
    onSnackSelected: (Long, String, NavBackStackEntry) -> Unit,
    onCheckout: () -> Unit,
    onOrderSelected: (Long) -> Unit,
) {
    val jetsnackScaffoldState = rememberJetsnackScaffoldState()
    val nestedNavController = rememberJetsnackNavController()
    val navBackStackEntry by nestedNavController.navController.currentBackStackEntryAsState()
    val currentRoute = navBackStackEntry?.destination?.route
    val sharedTransitionScope = LocalSharedTransitionScope.current
        ?: throw IllegalStateException("No SharedElementScope found")
    val animatedVisibilityScope = LocalNavAnimatedVisibilityScope.current
        ?: throw IllegalStateException("No SharedElementScope found")
    JetsnackScaffold(
        bottomBar = {
            with(animatedVisibilityScope) {
                with(sharedTransitionScope) {
                    JetsnackBottomBar(
                        tabs = HomeSections.entries.toTypedArray(),
                        currentRoute = currentRoute ?: HomeSections.FEED.route,
                        navigateToRoute = nestedNavController::navigateToBottomBarRoute,
                        modifier = Modifier
                            .renderInSharedTransitionScopeOverlay(
                                zIndexInOverlay = 1f,
                            )
                            .animateEnterExit(
                                enter = fadeIn(nonSpatialExpressiveSpring()) + slideInVertically(
                                    spatialExpressiveSpring(),
                                ) {
                                    it
                                },
                                exit = fadeOut(nonSpatialExpressiveSpring()) + slideOutVertically(
                                    spatialExpressiveSpring(),
                                ) {
                                    it
                                },
                            ),
                    )
                }
            }
        },
        modifier = modifier,
        snackbarHost = {
            SnackbarHost(
                hostState = it,
                modifier = Modifier.systemBarsPadding(),
                snackbar = { snackbarData -> JetsnackSnackbar(snackbarData) },
            )
        },
        snackBarHostState = jetsnackScaffoldState.snackBarHostState,
    ) { padding ->
        NavHost(
            navController = nestedNavController.navController,
            startDestination = HomeSections.FEED.route,
        ) {
            addHomeGraph(
                onSnackSelected = onSnackSelected,
                onCheckout = onCheckout,
                onOrderSelected = onOrderSelected,
                modifier = Modifier
                    .padding(padding)
                    .consumeWindowInsets(padding),
            )
        }
    }
}

val LocalNavAnimatedVisibilityScope = compositionLocalOf<AnimatedVisibilityScope?> { null }
val LocalSharedTransitionScope = compositionLocalOf<SharedTransitionScope?> { null }
