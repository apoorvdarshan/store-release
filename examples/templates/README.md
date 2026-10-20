# Templates

Starting points for a new app, with placeholder values (`Example App`, `com.example.app`,
App Store Connect id `1234567890`, team `ABCDE12345`, an all-zero upload certificate and the
all-zero commit `0000000000000000000000000000000000000000`):

| Template | Platforms | Tags |
|---|---|---|
| [ios-only](ios-only/) | App Store Connect | `v1.2` |
| [android-only](android-only/) | Google Play | `v1.2` |
| [ios-and-android](ios-and-android/) | both | `ios-v1.2`, `android-v1.2` |

Each holds `store-release.toml` (the app's `store/store-release.toml`), `callers/` (the app's
`.github/workflows/`) and `dependabot.yml` (the app's `.github/dependabot.yml`). They are exactly
what `run.py init` writes for those values, so a test fails when they drift.

Don't copy the callers by hand: the all-zero commit is not a release. Run `init` instead, from a
checkout of the [release](https://github.com/apoorvdarshan/store-release/releases/latest) you
want to pin (`vX.Y.Z` below), with your own values:

```bash
git clone --depth 1 --branch vX.Y.Z https://github.com/apoorvdarshan/store-release /tmp/store-release
python3 -I -u /tmp/store-release/run.py --app-root . init --platform all --name "My App" \
  --bundle-id com.example.myapp --asc-app-id 1234567890 --team-id ABCDE12345 \
  --project ios/MyApp.xcodeproj --package com.example.myapp --upload-cert-sha256 <sha-256>
```

It pins the callers to the release's commit, and every switch stays off until you set it
([quickstart](../../README.md#quickstart)).
