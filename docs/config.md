# `store/store-release.toml` (schema 1)

Each app keeps one file, `store/store-release.toml`, that tells store-release where
its store files are and what its fixed identities are (bundle id, package, team,
upload certificate). Switches stay in repository variables ([switches.md](switches.md)),
secrets in repository secrets ([secrets.md](secrets.md)). Nothing in the file is secret.

Worked examples: [Fud AI](../examples/fud-ai/store-release.toml), the five apps on a
ported flow ([Scowld](../examples/scowld/store-release.toml),
[Verceltics](../examples/verceltics/store-release.toml),
[ABYSM](../examples/abysm/store-release.toml),
[PCM Notes](../examples/pcm-notes/store-release.toml),
[Quit All](../examples/quit-all/store-release.toml); see [flows.md](flows.md)), and a
[non-Fud AI fixture](../examples/example/store-release.toml) that the tests use to
prove nothing app-specific is hard-coded.

Check a file locally:

```bash
python3 -I -u path/to/store-release/run.py --app-root . config check --files
```

## Rules

- **Format.** TOML 1.0, read with `tomllib`. Callers may pass `config:` to name
  another file; the default is `store/store-release.toml`.
- **Strict.** Unknown tables or keys, a wrong type (the string `"true"` is not a
  bool), an enum value this version does not accept, and an unsafe path are errors,
  reported with the dotted key (`ios.build.developer_dir: ...`).
- **Additive.** A config written for a newer store-release fails loudly on an older
  one, never silently. New keys arrive as optional, with defaults that keep today's
  behaviour; `schema` changes only for a breaking change.
- **Paths** are relative to the app root, `/`-separated, with no absolute paths, no
  empty, `.` or `..` segment, and no backslash, NUL or newline. When a path is used,
  the resolved path (following symlinks) must stay inside the app root.
- **Sparse patterns** (`*.checkout`) match `^!?/[^\n]*[^\s]$` with no `..` segment.
  They are non-cone gitignore patterns; their order is kept and they come after the
  derived paths, so a `!` negation wins.
- **Templates** accept only the placeholders listed for their key.
- **Hook keys** (marked *hook*) accept exactly one value in this version. They exist
  so a later release can add values without a schema change; the default is
  today's behaviour.
- **Pins** (marked *pin*) state facts about the app's own files that must not change
  by accident. `config check --files` (Store checks) and `config check --pinned`
  (every iOS prepare, before anything else) fail when one no longer holds, so a change
  is a reviewed edit of the pin. They replace the unit tests that pinned these facts
  in fud-ai's real files.
- **A switch needs its table.** A switch that is on while its table is missing fails
  in the gates, naming the table: `IOS_UPLOAD_LISTING is on, but
  store/store-release.toml has no [ios.listing]`. `IOS_SUBMIT_REVIEW` needs
  `[ios.products]`.
- At least one of `[ios]` and `[android]` must be present. The iOS workflows refuse
  a config without `[ios]`, the Android workflows one without `[android]`.

## Keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `schema` | int | required | must be `1` |
| `app.name` | str | required | 1–64 printable characters; used in messages (`this publisher only supports the <name> iOS bundle ...`) |
| `app.slug` | str | required | `^[a-z0-9][a-z0-9-]{0,31}$`; temp names `<slug>.xcarchive`, `<slug>-signing.keychain-db`, `<slug>-release.jks`, `<slug>-keystore.properties` |
| `release_notes.path` | path | `RELEASE_NOTES.md` | `## <exact tag>` sections become the GitHub Release body; a missing file or section means generated notes; an empty or duplicated section, or the tag `Unreleased`, fails |
| `release_notes.unique_headings` | bool | `true` | Store checks fail when any `## ` heading appears twice, not only the tag being released (Scowld, Verceltics and PCM Notes never checked that: `false`) |
| `catalog.path` | path | required in the table | the product catalog JSON |
| `catalog.groups` | list[str] | required | non-empty, unique, `^[A-Za-z_]+$`; each must be a list in the catalog. Only these ids are ever attached to App Review |
| `catalog.id_prefix` | str | required | every App Store product id starts with it |
| `catalog.play_rules` | bool | `false` | Play rules: a `play` block if and only if `android` is in `platforms`; id and base-plan formats; `revenuecat_product_id == "<sub>:<base>"` for the `subscriptions` group; Play ids unique |
| `catalog.revenuecat` | path | none | when set, `revenuecat-gate` also counts its `entitlements` and `offerings` |
| `catalog.checks` | list[path] | `[]` | app hook scripts (`.py`/`.sh`) run after the generic checks wherever `catalog check` runs |
| `catalog.storekit` / `.products_source` | path / path | none | a ported flow's catalog check compares the catalog with the app's StoreKit configuration and the source file that declares its product ids (`BillingModels.kt`, `StoreManager.swift`, `TipStore.swift`, `PaywallManager.swift`); for a ported flow `catalog.groups` must be the groups that flow reads |
| `checks.paths` | list[str] | `[]` | path filter of the app's `store-checks.yml` for `push` and `pull_request`; `[]` = no filter |
| `ios.flow` | enum | `fud-ai` | which release flow runs ([flows.md](flows.md)): `fud-ai` (store-release's own modules), or the ported `scowld`, `verceltics`, `abysm`, `pcm-notes`, `quit-all`. A ported flow accepts only the listing, What's New and screenshot keys it reads, and no `ios.products` or `ios.export_compliance` (it decides those itself) |
| `ios.tag_prefix` | str | required | `^([a-z]+-)*v$`; tags are `<tag_prefix><MARKETING_VERSION>` |
| `ios.bundle_id` | str | required | reverse-DNS; must resolve to exactly one App Store Connect app, and the archive's main bundle must carry it |
| `ios.asc_app_id` | str | required | `^[0-9]{6,12}$`; version creation refuses any other app |
| `ios.team_id` | str | required | `^[A-Z0-9]{10}$`; ExportOptions `teamID` |
| `ios.project` | path | required | the `*.xcodeproj`; its `project.pbxproj` holds `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION`, identical in every configuration |
| `ios.scheme` | str | required | the scheme to archive |
| `ios.app_target` | str | required | the target whose configurations decide export compliance |
| `ios.info_plist` | path | required (`fud-ai`) | the app target's Info.plist; `config check --files` checks it is the target's resolved `INFOPLIST_FILE`. Optional for a ported flow (an app whose Info.plist Xcode generates has none) |
| `ios.export_compliance` | enum | none | *pin*; `NO` or `YES`: the `ITSAppUsesNonExemptEncryption` answer every app-target configuration must declare (YES also needs `ITSEncryptionExportComplianceCode`). Unset accepts either valid answer |
| `ios.locale` | str | `en-US` | the listing, What's New and creative locale |
| `ios.gate_jobs` | array of tables | required | non-empty; each `{ id = "<job id>", uses = "./.github/workflows/<file>.yml" }`, optionally `with = { <input> = <string, bool or int> }` (rendered as the job's `with:`), ids unique, `^[a-z0-9][a-z0-9_-]*$`, not a store-release job name, `web`, `android`, `store-release` or `checks`. The app's own test workflows that must pass on a tag before anything after prepare runs, with `contents: read` |
| `ios.gate_policy` | enum | `parallel` | when the gate jobs run: `parallel` on every tag push next to prepare (`if: github.event_name == 'push'`); `after-prepare` the same, after prepare (Quit All); `when-uploading` after prepare and only on a tag run that uploads a build (PCM Notes: a listing-only tag run does not wait for them) |
| `ios.prepare.checks` | list[path] | `[]` | app scripts every iOS prepare runs before the switches are read, with a scrubbed environment plus `GITHUB_EVENT_NAME` and `GITHUB_REF_NAME` (Verceltics' repository tests) |
| `ios.prepare.checkout` | list[pattern] | none | what those scripts read, added to prepare's sparse checkout |
| `ios.build.developer_dir` | abs path | required | the Xcode to use; must not contain `beta` (any case) |
| `ios.build.checkout` | list[pattern] | required | the build job's sparse checkout (the config and the hook scripts are added automatically) |
| `ios.build.package_cache_files` | list[glob] | `["**/Package.resolved"]` | files hashed into the SPM-sources cache key |
| `ios.build.pre_archive` | list[path] | `[]` | app scripts run before signing material is installed, with a scrubbed environment plus `MARKETING_VERSION`, `BUILD_NUMBER`, `DEVELOPER_DIR` |
| `ios.build.post_archive` | list[path] | `[]` | app scripts run after the archive check and before export, with a scrubbed environment plus `ARCHIVE_PATH`, `APP_PATH`, `MARKETING_VERSION`, `BUILD_NUMBER`, `DEVELOPER_DIR` (PCM Notes' offline-notes check) |
| `ios.build.extensions` | list[str] | `[]` | the release bundle ids of every app extension; with any set (or `app_groups`), the archive must hold exactly one app and exactly these extensions, each signed for `team_id` with its app groups |
| `ios.build.app_groups` | table | `{}` | `"<bundle id>" = ["group.<...>"]`: the app groups each bundle's signature must carry (the app or one of `extensions`) |
| `ios.build.verify_ipa` | bool | `false` | export once with App Store distribution signing and verify the IPA (strict signature, an Apple Distribution certificate, an App Store profile with the app groups, no `get-task-allow`) before validating or uploading; a release run then exports again to upload (Quit All's `verify_ios_bundles.sh`). Needs `extensions` or `app_groups` |
| `ios.listing.source` | enum | `markdown` (`fud-ai`), `files` (ported) | `markdown`: the first fence under each fixed heading (`App Name`, `Subtitle (30 chars max)`, `Promotional Text (170 chars max)`, `Keywords (100 chars max)`, `Description`, `Privacy URL`, `Support URL`, `Marketing URL`); uploads description, keywords, promotional text, marketing and support URLs. `files`: one text file per field in `<dir>/<locale>/`, read by the app's flow ([flows.md](flows.md)) |
| `ios.listing.markdown` | path | `APPSTORE.md` | |
| `ios.listing.dir` | path | `store/metadata/ios` | `files` source: the folder holding one `<locale>/` folder per locale; its last segment is `ios` |
| `ios.whats_new.source` | enum | `markdown` | `markdown`: the first fence under the first `## ` line containing `heading`; at most 4,000 characters, plain text. Ported flows: `files` (`whats_new.txt` in each locale folder) or, for verceltics, `release-notes` (the tag's section converted to plain text) |
| `ios.whats_new.markdown` | path | `ios.listing.markdown` | |
| `ios.whats_new.heading` | str | `What's New` | substring match |
| `ios.whats_new.version_file` | str | none | the quit-all flow (required there): the file in the listing folder whose text must equal the release's version before What's New is uploaded |
| `ios.screenshots.sets` | array of tables | none | upload order; each has `display_type` (`APP_IPHONE_67`, `APP_IPAD_PRO_3GEN_129`, `APP_IPHONE_DUO`, `WATCH`), `dir` and `required`. Non-WATCH sets must have `required = true` (a missing or empty folder fails); the WATCH set must have `required = false` (staged only when its folder exists) and come last; display types are unique. Frames are numbered `NN-name.png` |
| `ios.screenshots.manifest` | str | none | the scowld and quit-all flows: the JSON file in each `<listing dir>/<locale>/` that lists the screenshots per display type (`watch` for Apple Watch frames), in order |
| `ios.creative.dir` | path | `store/creative/ios` | `header/` takes one 3840×1646 PNG; `search-results/` one 3:2 PNG or JPEG, 1920–3840 wide; no alpha |
| `ios.creative.sizes` | table | none | *pin*; `"<slot>/<file>" = <bytes>` for each image the live Asset Library holds (`header/` a `.png`, `search-results/` a `.png`, `.jpg` or `.jpeg`; one per slot). The slots must hold exactly these files at these sizes, and they must pass the image checks |
| `ios.products.iap_route` | enum | `submission` | *hook*; subscriptions as `subscriptionVersion` draft items, READY_TO_SUBMIT in-app purchases via `POST /inAppPurchaseSubmissions` after the final build recheck. The table itself is what `IOS_SUBMIT_REVIEW` needs |
| `ios.github_release.title` | template | `iOS {version} (build {build})` | `{version}`, `{tag}`; `{build}` is the uploaded build, else the project's `CURRENT_PROJECT_VERSION`; `{build_suffix}` is ` (build N)` only when this run uploaded build N |
| `ios.github_release.on_existing` | enum | `update` | a tag's existing GitHub Release: `update` its title, notes and flags; `upload-missing` keep it and attach the release's files again unless all are there (Scowld); `keep` it as it is (ABYSM, PCM Notes); `fail` (create only: Verceltics, Quit All) |
| `ios.github_release.latest` | enum | `highest` | `highest`: Latest only when the tag is the highest iOS version; `always`: every release is created with `--latest` |
| `ios.github_release.source_archive` | bool | `false` | a `source-archive` job (`ios-source-archive.yml`, the only one with `id-token: write`) archives the tagged commit as `<slug>-<version>-source.tar.gz` with its `.sha256` and a Sigstore bundle, attests its provenance, and the release attaches the three files ([verification.md](verification.md)) |
| `android.flow` | enum | `fud-ai` | `fud-ai`, or the ported `verceltics` ([flows.md](flows.md)) |
| `android.tag_prefix` | str | required | `^([a-z]+-)*v$`; the tag must equal `<prefix><versionName>` |
| `android.package` | str | required | the applicationId and Play `packageName` |
| `android.gradle_file` | path | `android/app/build.gradle.kts` | exactly one `versionName = "X"` and one `versionCode = N` line |
| `android.upload_cert_sha256` | str | required | `^[0-9a-f]{64}$`; the only allowed signer of the AAB and APK |
| `android.locale` | str | `en-US` | listing locale; the What's New file kept for upload is `whatsnew-<locale>` |
| `android.gate_jobs` | array of tables | required | as for iOS (`with` included); rendered with `needs: gates` and `if: needs.gates.outputs.mode == 'release' && needs.gates.outputs.build == 'true'` |
| `android.build.dir` | path | `android` | the Gradle working directory |
| `android.build.tasks` | list[str] | `[":app:bundleRelease", ":app:assembleRelease"]` | each `^:?[A-Za-z0-9_.:-]+$`; always run with `--no-build-cache` |
| `android.build.aab` | path | `<dir>/app/build/outputs/bundle/release/app-release.aab` | |
| `android.build.apk` | path | `<dir>/app/build/outputs/apk/release/app-release.apk` | |
| `android.build.checkout` | list[pattern] | required | the release job's sparse checkout |
| `android.build.signing` | enum | required | `keystore-properties` (the `fud-ai` flow): the four keystore secrets become `storeFile`, `storePassword`, `keyAlias`, `keyPassword` (Java-properties escaped) in `RUNNER_TEMP/<slug>-keystore.properties`, mode 600, linked at `keystore_properties`. `gradle-properties` (the `verceltics` flow): the keystore and a file holding its password, under `RUNNER_TEMP`, reach Gradle as `-P<keystore_property>=...` and `-P<password_file_property>=...`; only `ANDROID_KEYSTORE_BASE64` and `ANDROID_KEYSTORE_PASSWORD` are used |
| `android.build.keystore_property` / `.password_file_property` / `.key_alias` | str / str / str | required for `gradle-properties` | the two Gradle property names, and the key alias `android verify` checks |
| `android.build.sdk_packages` | list[str] | `[]` | Android SDK packages `setup-android` installs before the build (none: the runner's own SDK) |
| `android.build.keystore_properties` | path | `<dir>/keystore.properties` | where Gradle reads the signing config; the app must gitignore it |
| `android.listing.source` / `.markdown` | enum / path | `markdown` / `PLAYSTORE.md` | headings `1. App Name`, `2. Short Description`, `3. Full Description`; at most 30/80/4,000 characters. The verceltics flow: `files` |
| `android.listing.dir` | path | `store/metadata/play` | `files` source: one `<locale>/` folder of text files |
| `android.whats_new.source` / `.markdown` / `.heading` | enum / path / str | `markdown` / `PLAYSTORE.md` / `What's New` | `<locale>...</locale>` blocks of at most 500 characters, plain text. The verceltics flow: `release-notes` (the tag's section, as plain text) |
| `android.screenshots.dir` | path | required in the table | numbered `NN-name.png` phone frames |
| `android.screenshots.pad_to_9_16` | bool | `true` | `true` (the `fud-ai` flow): off-aspect frames are padded onto black at 9:16, then validated; `false` (the `verceltics` flow): the frames are uploaded as they are |
| `android.github_release.asset_name` | template | `{slug}-v{version}` | `{slug}`, `{version}`, `{version_code}`; must render to `^[A-Za-z0-9._-]+$`; names the `.aab`, `.apk` and their `.sigstore.json`. The title is fixed: `Android {version} (versionCode {version_code})` |

## Fixed in code, not config

These stay in code for the fud-ai flow (a ported flow keeps its app's own tables):

| Display type | Staged folder | Accepted sizes (the first is the fit target) |
|---|---|---|
| `APP_IPHONE_67` | `ios/6.7` | 1290×2796, 1284×2778 |
| `APP_IPAD_PRO_3GEN_129` | `ios/ipad-13` | 2064×2752, 2048×2732 |
| `APP_IPHONE_DUO` | `ios/iphone-duo` | 1398×2034, 2007×2853 |
| `WATCH` | `ios/watch` | 416×496 `APP_WATCH_SERIES_10`, 422×514 and 410×502 `APP_WATCH_ULTRA`, 396×484 `APP_WATCH_SERIES_7`, 368×448 `APP_WATCH_SERIES_4`, 312×390 `APP_WATCH_SERIES_3` |
| Play phone | `play/phone` | portrait, 9:16 ± 0.08, each side 320–3840; at most 8 staged |

Also fixed: the App Store version and creative state sets and timings, at most 10
screenshots per set, the build poll and wait ranges, 600 s upload clock skew, the
JWT lifetime (1200 s, refreshed after 600 s), HTTP timeouts, the transient status
set, the PNG checks, the Play track mapping, the release title format, the Apple
WWDR G3 certificate and bundletool 1.18.3 checksums, and the export options.
