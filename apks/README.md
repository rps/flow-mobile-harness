# Pinned third-party APKs

APK files are git-ignored (`apks/*.apk`). Download them to this folder and check the hash before use; `harness/emulator/manager.py` refuses to install on a mismatch.

| App | Package | Version | Source | SHA-256 |
| --- | --- | --- | --- | --- |
| Markor | net.gsantner.markor | 2.16.1 (versionCode 163, flavorDefault, released 2026-03-19) | https://github.com/gsantner/markor/releases/download/v2.16.1/net.gsantner.markor-v163-2.16.1-flavorDefault-release.apk | e88cdcced7aa3dca25e6b9c7a9bdcfad3e3988ee545be951f42bf9441b5e46bf |

Source: the official GitHub releases of gsantner/markor (latest release at download time, 2026-10-02). The hash was computed locally with `shasum -a 256` and matches the digest GitHub publishes for that release asset.

```
curl -L -o apks/net.gsantner.markor-v163-2.16.1-flavorDefault-release.apk \
  https://github.com/gsantner/markor/releases/download/v2.16.1/net.gsantner.markor-v163-2.16.1-flavorDefault-release.apk
shasum -a 256 apks/*.apk
```
