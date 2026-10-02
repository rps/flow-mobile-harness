# Building

Derived from Google's Jetsnack sample (`github.com/android/compose-samples`, directory
`Jetsnack`, commit `fe26402`, Apache 2.0; fonts under the SIL OFL, see `ASSETS_LICENSE`).
Licence headers are kept. The Kotlin package stays `com.example.jetsnack`; only the
application id is `com.labs.snackorders`.

## Requirements

- Android SDK at `~/Library/Android/sdk` (`ANDROID_HOME`). The build needs platform
  `android-37`; the Android Gradle plugin downloads it on first build if it is missing.
- Java 21. The machine's default `java` is Temurin 25; this project was built with the
  runtime bundled in Android Studio:
  `/Applications/Android Studio.app/Contents/jbr/Contents/Home` (OpenJDK 21.0.9).
  Whether Java 25 works with Gradle 9.5 / AGP 9.3.1 was not tried.
- Gradle 9.5.0 is fetched by the wrapper on first run.

## Build

```sh
cd sample-app
JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
ANDROID_HOME="$HOME/Library/Android/sdk" \
./gradlew :app:assembleDebug
cp app/build/outputs/apk/debug/app-debug.apk dist/snackorders-debug.apk
```

Format check used as a gate (ktlint via Spotless, as upstream):

```sh
JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew spotlessCheck
```

## Install

```sh
adb -s <serial> install -r dist/snackorders-debug.apk
adb -s <serial> shell am start -n com.labs.snackorders/com.example.jetsnack.ui.MainActivity
```

The APK is debuggable, so `adb shell run-as com.labs.snackorders` works for pushing the
seed (`SEED_FORMAT.md`) and pulling the database (`SCHEMA.md`).

## Test emulator used

AVD `snackorders_test_api36` (created for this work; existing AVDs were not used), from
`system-images;android-36.1;google_apis;arm64-v8a`, device profile `pixel_7`:

```sh
avdmanager create avd -n snackorders_test_api36 -k "system-images;android-36.1;google_apis;arm64-v8a" -d pixel_7
emulator -avd snackorders_test_api36 -port 5580 -no-snapshot-save -no-boot-anim -no-window -no-audio
```

If `avdmanager` writes `target=android-0` into `~/.android/avd/snackorders_test_api36.ini`
(it did here), change it to `target=android-36.1`; otherwise the emulator hangs at cold boot
and crashes. `config.ini` was also given `hw.ramSize=3072M` and
`disk.dataPartition.size=6442450944`.

The device is then `emulator-5580`.

## Changes from upstream worth knowing

- `INTERNET` permission removed; the app has no network code path at all.
- The home-screen widget (`RecentOrdersWidgetReceiver`) is no longer registered in the
  manifest: it showed fake demo "recent orders" that are not in the database. Its source is
  still in the tree.
- The cart's deliberate "every fifth quantity change fails" behaviour was removed.
- `SnackRepo` and `SearchRepo` read from the database (`data/Store.kt`); the static lists
  in `Snack.kt` are now only the built-in catalogue and preview data.

## Optional future tests (not implemented)

- Re-registering the widget to test the agent against a stale secondary surface.
- Re-enabling the periodic cart failure (seed-controlled) to test recovery from transient errors.
