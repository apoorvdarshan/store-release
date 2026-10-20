# Roadmap

Planned, not promised; each item lands as its own release with a CHANGELOG entry. How the
current features came about: [provenance.md](provenance.md).

## Planned

- **One engine for the ported flows.** The five [ported flows](flows.md) are their apps'
  own scripts. Folding their differences into the standard flow as config keys, one app
  at a time, with that app's own test suite and inventory as the safety net, would let any
  app pick those behaviours (per-locale listing files, screenshot manifests, review-item
  products, What's New repeat guards).
- **Sigstore signing outside the Gradle job**, so app build code never runs in a job with
  `id-token: write`.
- **Uploading the verified IPA** instead of exporting it twice when
  `ios.build.verify_ipa` is on.
- **`store-release` as a required check** per app (drop the `checks.paths` filter first).
- Fuzz targets for the ported flows' parsers.
- Suggested tag rulesets for apps (`ios-v*`, `android-v*`).

## Known gaps

Not supported in this version; issues and pull requests are welcome
([CONTRIBUTING.md](../CONTRIBUTING.md)):

- `.xcworkspace` (CocoaPods) projects: `ios.project` must be an `.xcodeproj`.
- Groovy Gradle files: the version lines must be Kotlin DSL (`versionName = "1.2"`).
- macOS, tvOS and watchOS-only apps: the build archives for `generic/platform=iOS`.
- RevenueCat sync: `STORE_SYNC_REVENUECAT` fails closed, because the write path is not
  implemented.
