# `store/store-release.toml` reference (schema 1)

Each app keeps one file, `store/store-release.toml`, that tells store-release where the
app's store files are and what its fixed identities are (bundle id, package, team, upload
certificate). Switches are repository variables ([switches.md](switches.md)) and
credentials are repository secrets ([secrets.md](secrets.md)); nothing in this file is
secret.

Start from `run.py init` (it writes this file and the callers, see the
[quickstart](../README.md#quickstart)) or from a [template](../examples/templates/). Check a
file locally:

```bash
python3 -I -u path/to/store-release/run.py --app-root . config check --files
```

Every key below is checked against the code: `tests/workflows/test_docs.py` fails when the
parser reads a key this page does not list, or this page lists one the parser does not read.

## Rules

- **Format.** TOML 1.0, read with `tomllib`. Callers may pass `config:` to name another
  file; the default is `store/store-release.toml`.
- **Strict.** Unknown tables or keys, a wrong type (the string `"true"` is not a bool), an
  enum value this version does not accept, and an unsafe path are errors, reported with
  the dotted key (`ios.build.developer_dir: ...`).
- **Additive.** A config written for a newer store-release fails loudly on an older one,
  never silently. New keys arrive as optional, with defaults that keep today's behaviour;
  `schema` changes only for a breaking change.
- **Paths** are relative to the app root, `/`-separated, with no absolute paths, no empty,
  `.` or `..` segment, and no backslash, NUL or newline. When a path is used, the resolved
  path (following symlinks) must stay inside the app root.
- **Sparse patterns** (`*.checkout`) match `^!?/[^\n]*[^\s]$` with no `..` segment. They
  are non-cone gitignore patterns; their order is kept and they come after the paths
  store-release adds itself (the config, the project file, the hooks), so a `!` negation
  wins.
- **Templates** accept only the placeholders listed for their key.
- **Hook keys** (marked *hook*) accept exactly one value in this version. They exist so a
  later release can add values without a schema change.
- **Pins** (marked *pin*) state facts about the app's own files that must not change by
  accident. `config check --files` (Store checks) and `config check --pinned` (every iOS
  prepare, before anything else) fail when one no longer holds, so a change is a reviewed
  edit of the pin.
- **A switch needs its table.** A switch that is on while its table is missing fails the
  gates, naming the table: `IOS_UPLOAD_LISTING is on, but store/store-release.toml has no
  [ios.listing]`. `IOS_SUBMIT_REVIEW` needs `[ios.products]`. Without the table, Store
  checks skip that step.
- At least one of `[ios]` and `[android]` must be present. The iOS workflows refuse a
  config without `[ios]`, the Android workflows one without `[android]`.
- **Flows.** Every new app uses the standard flow (the default). The keys marked
  *ported* exist for the [ported flows](flows.md) of the apps store-release was built
  for; the standard flow refuses them or ignores them as noted.

## A complete example

Both platforms, with every optional table a typical app turns on. Comments name the
switch each table serves.

```toml
schema = 1

[app]
name = "My App"
slug = "my-app"

[release_notes]
path = "RELEASE_NOTES.md"            # "## ios-v1.2" sections become the GitHub Release notes

[ios]
tag_prefix = "ios-v"
bundle_id = "com.example.myapp"
asc_app_id = "1234567890"
team_id = "ABCDE12345"
project = "ios/MyApp.xcodeproj"
scheme = "MyApp"
app_target = "MyApp"
info_plist = "ios/MyApp/Info.plist"
export_compliance = "NO"
gate_jobs = [
  { id = "ios-tests", uses = "./.github/workflows/ios-tests.yml" },
]

[ios.build]
developer_dir = "/Applications/Xcode_27.app/Contents/Developer"
checkout = ["/ios/"]

[ios.listing]                         # IOS_UPLOAD_LISTING
[ios.whats_new]                       # IOS_UPLOAD_WHATS_NEW

[[ios.screenshots.sets]]              # IOS_UPLOAD_SCREENSHOTS
display_type = "APP_IPHONE_67"
dir = "store/screenshots/iphone"
required = true

[[ios.screenshots.sets]]
display_type = "APP_IPAD_PRO_3GEN_129"
dir = "store/screenshots/ipad"
required = true

[ios.creative]                        # IOS_UPLOAD_CREATIVE_ASSETS
[ios.products]                        # IOS_SUBMIT_REVIEW

[android]
tag_prefix = "android-v"
package = "com.example.myapp"
upload_cert_sha256 = "<64 lowercase hex>"
gate_jobs = [
  { id = "android-tests", uses = "./.github/workflows/android-tests.yml" },
]

[android.build]
checkout = ["/android/"]
signing = "keystore-properties"

[android.listing]                     # ANDROID_UPLOAD_LISTING
[android.whats_new]                   # ANDROID_UPLOAD_WHATS_NEW
[android.screenshots]                 # ANDROID_UPLOAD_SCREENSHOTS
dir = "store/screenshots/play"
```

## Keys

Types: *str*, *int*, *bool*, *path* (the path rules above), *abs path*, *pattern* (a sparse
pattern), *template*, *enum* (one of the listed strings), *list[...]*, *table*. "required"
means the key must be present when its table is.

### Top level and `[app]`

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `schema` | int | required | `1` | must be `1` |
| `app.name` | str | required | `"My App"` | 1–64 printable characters; used in messages (`this publisher only supports the <name> iOS bundle ...`) and store plans |
| `app.slug` | str | required | `"my-app"` | `^[a-z0-9][a-z0-9-]{0,31}$`; temporary names `<slug>.xcarchive`, `<slug>-signing.keychain-db`, `<slug>-release.jks`, `<slug>-keystore.properties`, the default Android asset name and the source archive name |

### `[release_notes]`

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `release_notes.path` | path | `"RELEASE_NOTES.md"` | `"CHANGELOG.md"` | `## <exact tag>` sections become the GitHub Release body; a missing file or section means GitHub's generated notes; an empty or duplicated section, or a tag named `Unreleased`, fails |
| `release_notes.unique_headings` | bool | `true` | `false` | Store checks fail when any `## ` heading appears twice, not only the tag being released; `false` checks only the tag's own section |

### `[catalog]` (optional: in-app products)

The product catalog, a JSON file in the app that lists the products each group holds;
review submission attaches only these ids.

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `catalog.path` | path | required | `"store/catalog/products.json"` | the product catalog JSON |
| `catalog.groups` | list[str] | required | `["subscriptions", "tips"]` | non-empty, unique, `^[A-Za-z_]+$`; each must be a list in the catalog. Only these ids are ever attached to App Review |
| `catalog.id_prefix` | str | required | `"com.example.myapp."` | every App Store product id starts with it |
| `catalog.play_rules` | bool | `false` | `true` | Play rules: a `play` block if and only if `android` is in `platforms`; id and base-plan formats; `revenuecat_product_id == "<sub>:<base>"` for the `subscriptions` group; Play ids unique |
| `catalog.revenuecat` | path | none | `"store/catalog/revenuecat.json"` | when set, `revenuecat-gate` also counts its `entitlements` and `offerings` |
| `catalog.checks` | list[path] | `[]` | `["scripts/ci/check_catalog.py"]` | app hook scripts (`.py`/`.sh`) run after the generic checks wherever `catalog check` runs, with a scrubbed environment |
| `catalog.storekit` | path | none | `"ios/MyApp/Products.storekit"` | *ported*: the StoreKit configuration a ported flow's catalog check compares the catalog with |
| `catalog.products_source` | path | none | `"ios/MyApp/StoreManager.swift"` | *ported*: the source file that declares the product ids a ported flow's catalog check compares; for a ported flow `catalog.groups` must be the groups that flow reads |

### `[checks]`

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `checks.paths` | list[str] | `[]` | `["store/**", "APPSTORE.md", "ios/MyApp.xcodeproj/project.pbxproj"]` | the `paths:` filter of the app's `store-checks.yml` for `push` and `pull_request`; `[]` = no filter (run on every change) |

### `[ios]`

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.flow` | enum | `"standard"` | `"standard"` | which release flow runs ([flows.md](flows.md)): `standard` (store-release's own modules; every new app uses it; `fud-ai` is its old name and still accepted), or the *ported* `scowld`, `verceltics`, `abysm`, `pcm-notes`, `quit-all`. A ported flow accepts only the listing, What's New and screenshot keys it reads, and no `ios.products` or `ios.export_compliance` |
| `ios.tag_prefix` | str | required | `"ios-v"` | `^([a-z]+-)*v$`; tags are `<tag_prefix><MARKETING_VERSION>` |
| `ios.bundle_id` | str | required | `"com.example.myapp"` | reverse-DNS; must resolve to exactly one App Store Connect app, and the archive's main bundle must carry it |
| `ios.asc_app_id` | str | required | `"1234567890"` | `^[0-9]{6,12}$`, the app's Apple ID (App Store Connect → App Information); version creation refuses any other app |
| `ios.team_id` | str | required | `"ABCDE12345"` | `^[A-Z0-9]{10}$`; the ExportOptions `teamID`, and the team every bundle must be signed for |
| `ios.project` | path | required | `"ios/MyApp.xcodeproj"` | the `*.xcodeproj`; its `project.pbxproj` holds `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION`, identical in every configuration of the app target |
| `ios.scheme` | str | required | `"MyApp"` | the shared scheme to archive (Release configuration) |
| `ios.app_target` | str | required | `"MyApp"` | the target whose configurations decide the version, the Info.plist and export compliance |
| `ios.info_plist` | path | none | `"ios/MyApp/Info.plist"` | the app target's Info.plist; `config check --files` checks it is the target's resolved `INFOPLIST_FILE` in every configuration. Leave it out when Xcode generates the whole Info.plist (`GENERATE_INFOPLIST_FILE = YES` and no `INFOPLIST_FILE`); the standard flow then checks that no configuration sets one |
| `ios.export_compliance` | enum | none | `"NO"` | *pin*; `NO` or `YES`: the `ITSAppUsesNonExemptEncryption` answer every app-target configuration must declare (`YES` also needs `ITSEncryptionExportComplianceCode`). Unset accepts either valid answer; a missing answer always fails the build |
| `ios.locale` | str | `"en-US"` | `"en-GB"` | the listing, What's New and creative locale |
| `ios.gate_jobs` | list[table] | required | `[{ id = "ios-tests", uses = "./.github/workflows/ios-tests.yml" }]` | non-empty: the app's own test workflows (`on: workflow_call`) that must pass on a tag before anything after prepare runs; each runs with `contents: read` |
| `ios.gate_jobs[].id` | str | required | `"ios-tests"` | the caller job id: unique, `^[a-z0-9][a-z0-9_-]*$`, not a store-release job name (`prepare`, `build`, ...), `web`, `android`, `store-release` or `checks` |
| `ios.gate_jobs[].uses` | str | required | `"./.github/workflows/ios-tests.yml"` | a workflow file in the app, `./.github/workflows/<file>.yml` |
| `ios.gate_jobs[].with` | table | `{}` | `{ simulator = "iPhone 17" }` | inputs rendered as the job's `with:`; string, bool or integer values, plain text without quotes, `$` or newlines |
| `ios.gate_policy` | enum | `"parallel"` | `"after-prepare"` | when the gate jobs run: `parallel` on every tag push next to prepare; `after-prepare` the same, after prepare; `when-uploading` after prepare and only on a tag run that uploads a build (a listing-only tag run does not wait for them) |

### `[ios.build]` (required)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.build.developer_dir` | abs path | required | `"/Applications/Xcode_27.app/Contents/Developer"` | the Xcode to use; must not contain `beta` (any case): App Store builds need a released Xcode |
| `ios.build.runner` | str | the build workflow's `xcode-27` | `"macos-26"` | the GitHub Actions runner label the build job runs on (GitHub-hosted or self-hosted), `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`; rendered into the caller only when set. Its Xcode must be at `developer_dir` |
| `ios.build.checkout` | list[pattern] | required | `["/ios/", "!/ios/Fixtures/"]` | the build job's sparse checkout: the project and everything it compiles (the config, the project file and the hook scripts are added automatically) |
| `ios.build.package_cache_files` | list[glob] | `["**/Package.resolved"]` | `["ios/**/Package.resolved"]` | files hashed into the Swift Package Manager sources cache key (with the Xcode version) |
| `ios.build.pre_archive` | list[path] | `[]` | `["scripts/ci/prepare_build.sh"]` | app scripts run before signing material is installed, with a scrubbed environment plus `MARKETING_VERSION`, `BUILD_NUMBER`, `DEVELOPER_DIR` |
| `ios.build.post_archive` | list[path] | `[]` | `["scripts/ci/check_archive.sh"]` | app scripts run after the archive check and before export, with a scrubbed environment plus `ARCHIVE_PATH`, `APP_PATH`, `MARKETING_VERSION`, `BUILD_NUMBER`, `DEVELOPER_DIR` |
| `ios.build.extensions` | list[str] | `[]` | `["com.example.myapp.widget"]` | the release bundle ids of every app extension; with any set (or `app_groups`), the archive must hold exactly one app and exactly these extensions, each with its bundle id, version and build, signed for `team_id` |
| `ios.build.app_groups` | table | `{}` | `{ "com.example.myapp" = ["group.com.example.myapp"] }` | `"<bundle id>" = ["group.<...>"]`: the app groups each bundle's signature must carry (the app or one of `extensions`) |
| `ios.build.verify_ipa` | bool | `false` | `true` | export once with App Store distribution signing and verify the IPA (strict signature, an Apple Distribution certificate, an App Store profile with the app groups, no `get-task-allow`) before validating or uploading; a release run then exports again to upload. Needs `extensions` or `app_groups` |

### `[ios.prepare]` (optional)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.prepare.checks` | list[path] | `[]` | `["scripts/ci/repo_tests.sh"]` | app scripts every iOS prepare runs before the switches are read, with a scrubbed environment plus `GITHUB_EVENT_NAME` and `GITHUB_REF_NAME` |
| `ios.prepare.checkout` | list[pattern] | none | `["/scripts/ci/", "/ios/MyApp/"]` | what those scripts read, added to prepare's sparse checkout |

### `[ios.listing]` (`IOS_UPLOAD_LISTING`)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.listing.source` | enum | `"markdown"` (standard), `"files"` (ported) | `"markdown"` | `markdown`: the first code fence under each fixed `## ` heading of the file: `App Name`, `Subtitle (30 chars max)`, `Promotional Text (170 chars max)`, `Keywords (100 chars max)`, `Description`, `Privacy URL`, `Support URL`, `Marketing URL`; uploads the description, keywords, promotional text and the marketing and support URLs. `files` (*ported*): one text file per field in `<dir>/<locale>/` |
| `ios.listing.markdown` | path | `"APPSTORE.md"` | `"store/APPSTORE.md"` | the listing document (`markdown` source) |
| `ios.listing.dir` | path | `"store/metadata/ios"` | `"store/metadata/ios"` | *ported* (`files` source): the folder holding one `<locale>/` folder per locale; its last segment is `ios` |

### `[ios.whats_new]` (`IOS_UPLOAD_WHATS_NEW`)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.whats_new.source` | enum | `"markdown"` | `"markdown"` | `markdown`: the first code fence under the first `## ` line containing `heading`; at most 4,000 characters of plain text. *Ported*: `files` (`whats_new.txt` in each locale folder) or, for the verceltics flow, `release-notes` (the tag's section as plain text) |
| `ios.whats_new.markdown` | path | `ios.listing.markdown`, else `"APPSTORE.md"` | `"APPSTORE.md"` | the document holding the What's New section |
| `ios.whats_new.heading` | str | `"What's New"` | `"Release notes"` | matched as a substring of the `## ` heading line |
| `ios.whats_new.version_file` | str | none | `"whats_new_version.txt"` | *ported*, the quit-all flow (required there): the file in the listing folder whose text must equal the release's version before What's New is uploaded |

### `[[ios.screenshots.sets]]` (`IOS_UPLOAD_SCREENSHOTS`)

One table per display type, in upload order. Frames are `NN-name.png`, staged as RGB without
alpha and fitted to the first accepted size ([fixed in code](#fixed-in-code-not-config)).

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.screenshots.sets` | list[table] | required in `[ios.screenshots]` | `[{ display_type = "APP_IPHONE_67", dir = "store/screenshots/iphone", required = true }]` | non-empty; display types unique; at least one iPhone or iPad set; the `WATCH` set comes last. A set is replaced only when its files differ |
| `ios.screenshots.sets[].display_type` | enum | required | `"APP_IPHONE_67"` | `APP_IPHONE_67`, `APP_IPAD_PRO_3GEN_129`, `APP_IPHONE_DUO` or `WATCH` |
| `ios.screenshots.sets[].dir` | path | required | `"store/screenshots/iphone"` | the folder of numbered frames |
| `ios.screenshots.sets[].required` | bool | required | `true` | non-`WATCH` sets: `true` (a missing or empty folder fails); the `WATCH` set: `false` (staged only when its folder exists) |
| `ios.screenshots.manifest` | str | none | `"screenshots.json"` | *ported*, the scowld and quit-all flows: the JSON file in each `<listing dir>/<locale>/` that lists the screenshots per display type, in order (instead of `sets`) |

### `[ios.creative]` (`IOS_UPLOAD_CREATIVE_ASSETS`)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.creative.dir` | path | `"store/creative/ios"` | `"store/creative/ios"` | `header/` holds one 3840×1646 PNG (the product page header), `search-results/` one 3:2 PNG or JPEG 1920–3840 wide; no alpha channel |
| `ios.creative.sizes` | table | none | `{ "header/header.png" = 1763332 }` | *pin*; `"<slot>/<file>" = <bytes>` for each image the live Asset Library holds (`header/` a `.png`, `search-results/` a `.png`, `.jpg` or `.jpeg`; one per slot). App Store Connect matches images by name and size, so the slots must hold exactly these files at these sizes, and they must pass the image checks |

### `[ios.products]` (`IOS_SUBMIT_REVIEW`)

The table itself is what `IOS_SUBMIT_REVIEW` needs (an empty table is fine).

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.products.iap_route` | enum | `"submission"` | `"submission"` | *hook*; subscriptions go into the version's review draft as `subscriptionVersion` items, and Ready-to-Submit in-app purchases go through `POST /inAppPurchaseSubmissions` after the final build recheck |

### `[ios.github_release]`

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `ios.github_release.title` | template | `"iOS {version} (build {build})"` | `"My App {version}"` | placeholders `{version}`, `{tag}`, `{build}` (the uploaded build, else the project's `CURRENT_PROJECT_VERSION`) and `{build_suffix}` (` (build N)` only when this run uploaded build N) |
| `ios.github_release.on_existing` | enum | `"update"` | `"keep"` | a tag's existing GitHub Release: `update` its title, notes and flags; `upload-missing` keep it and attach the release's files again unless all are there; `keep` it as it is; `fail` (create only) |
| `ios.github_release.latest` | enum | `"highest"` | `"always"` | `highest`: marked Latest only when the tag is the highest iOS version; `always`: every release is created with `--latest` |
| `ios.github_release.source_archive` | bool | `false` | `true` | a `source-archive` job (`ios-source-archive.yml`, the only iOS job with `id-token: write`) archives the tagged commit as `<slug>-<version>-source.tar.gz` with its `.sha256` and a Sigstore bundle, attests its provenance, and the release attaches the three files ([verification.md](verification.md)) |

### `[android]`

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `android.flow` | enum | `"standard"` | `"standard"` | `standard` (`fud-ai` is its old name), or the *ported* `verceltics` ([flows.md](flows.md)) |
| `android.tag_prefix` | str | required | `"android-v"` | `^([a-z]+-)*v$`; the tag must equal `<tag_prefix><versionName>` |
| `android.package` | str | required | `"com.example.myapp"` | the applicationId and Play `packageName` |
| `android.gradle_file` | path | `"android/app/build.gradle.kts"` | `"app/build.gradle.kts"` | exactly one `versionName = "X"` and one `versionCode = N` line (Kotlin DSL) |
| `android.upload_cert_sha256` | str | required | `"0f3e5a8c1b7d29e46a0c5b3f8d1e7a9c2b4d6f8e0a1c3e5f7b9d1f3a5c7e9b1d"` | `^[0-9a-f]{64}$`: the SHA-256 of the Play upload key's certificate (Play Console → Test and release → App integrity), the only allowed signer of the AAB and APK |
| `android.locale` | str | `"en-US"` | `"en-GB"` | the listing locale; What's New is kept for upload as `whatsnew-<locale>` |
| `android.gate_jobs` | list[table] | required | `[{ id = "android-tests", uses = "./.github/workflows/android-tests.yml" }]` | as `ios.gate_jobs`; rendered with `needs: gates` and run only when a tag run builds |
| `android.gate_jobs[].id` | str | required | `"android-tests"` | as `ios.gate_jobs[].id` (also not `gates`, `release` or `store-plan`) |
| `android.gate_jobs[].uses` | str | required | `"./.github/workflows/android-tests.yml"` | as `ios.gate_jobs[].uses` |
| `android.gate_jobs[].with` | table | `{}` | `{ android-release = true }` | as `ios.gate_jobs[].with` |

### `[android.build]` (required)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `android.build.dir` | path | `"android"` | `"apps/android"` | the Gradle working directory (a folder, never the repository root) |
| `android.build.tasks` | list[str] | `[":app:bundleRelease", ":app:assembleRelease"]` | `[":app:bundleProdRelease", ":app:assembleProdRelease"]` | each `^:?[A-Za-z0-9_.:-]+$`; always run with `--no-build-cache` |
| `android.build.aab` | path | `"<dir>/app/build/outputs/bundle/release/app-release.aab"` | `"android/app/build/outputs/bundle/prodRelease/app-prod-release.aab"` | the AAB the tasks produce |
| `android.build.apk` | path | `"<dir>/app/build/outputs/apk/release/app-release.apk"` | `"android/app/build/outputs/apk/prod/release/app-prod-release.apk"` | the APK the tasks produce |
| `android.build.checkout` | list[pattern] | required | `["/android/"]` | the release job's sparse checkout |
| `android.build.signing` | enum | required | `"keystore-properties"` | `keystore-properties` (the standard flow): the four keystore secrets become `storeFile`, `storePassword`, `keyAlias`, `keyPassword` (Java-properties escaped) in `RUNNER_TEMP/<slug>-keystore.properties`, mode 600, linked at `keystore_properties`. `gradle-properties` (*ported*, the verceltics flow): the keystore and a file holding its password reach Gradle as `-P<keystore_property>=...` and `-P<password_file_property>=...`; only `ANDROID_KEYSTORE_BASE64` and `ANDROID_KEYSTORE_PASSWORD` are used |
| `android.build.keystore_properties` | path | `"<dir>/keystore.properties"` | `"android/keystore.properties"` | where Gradle reads the signing config; the app must gitignore it |
| `android.build.keystore_property` | str | required for `gradle-properties` | `"releaseStoreFile"` | *ported*: the Gradle property that names the keystore file |
| `android.build.password_file_property` | str | required for `gradle-properties` | `"releaseStorePasswordFile"` | *ported*: the Gradle property that names the file holding the keystore password |
| `android.build.key_alias` | str | required for `gradle-properties` | `"upload"` | *ported*: the key alias `android verify` checks |
| `android.build.sdk_packages` | list[str] | `[]` | `["platform-tools", "platforms;android-37.0"]` | Android SDK packages `setup-android` installs before the build (none: the runner's own SDK) |

### `[android.listing]` (`ANDROID_UPLOAD_LISTING`)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `android.listing.source` | enum | `"markdown"` (standard), `"files"` (ported) | `"markdown"` | `markdown`: the code fences under the headings `1. App Name`, `2. Short Description`, `3. Full Description`; at most 30, 80 and 4,000 characters. `files` (*ported*): one `<locale>/` folder of text files |
| `android.listing.markdown` | path | `"PLAYSTORE.md"` | `"store/PLAYSTORE.md"` | the listing document (`markdown` source) |
| `android.listing.dir` | path | `"store/metadata/play"` | `"store/metadata/play"` | *ported* (`files` source): the folder holding one `<locale>/` folder; its last segment is `play` |

### `[android.whats_new]` (`ANDROID_UPLOAD_WHATS_NEW`)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `android.whats_new.source` | enum | `"markdown"` (standard), `"release-notes"` (ported) | `"markdown"` | `markdown`: `<locale>...</locale>` blocks of at most 500 characters of plain text in the code fence under the `heading` section. `release-notes` (*ported*): the tag's release-notes section as plain text |
| `android.whats_new.markdown` | path | `"PLAYSTORE.md"` | `"PLAYSTORE.md"` | the document holding the What's New section |
| `android.whats_new.heading` | str | `"What's New"` | `"Release notes"` | matched as a substring of the `## ` heading line |

### `[android.screenshots]` (`ANDROID_UPLOAD_SCREENSHOTS`)

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `android.screenshots.dir` | path | required in the table | `"store/screenshots/play"` | numbered `NN-name.png` phone frames; the whole Play set is replaced, at most 8 |
| `android.screenshots.pad_to_9_16` | bool | `true` (standard), `false` (ported) | `true` | `true`: off-aspect frames are padded onto black at 9:16, then validated; `false` (*ported*, the only value the verceltics flow accepts): uploaded as they are |

### `[android.github_release]`

| Key | Type | Default | Example | Meaning |
|---|---|---|---|---|
| `android.github_release.asset_name` | template | `"{slug}-v{version}"` | `"{slug}-{version}-{version_code}"` | placeholders `{slug}`, `{version}`, `{version_code}`; must render to `^[A-Za-z0-9._-]+$`; names the `.aab`, `.apk` and their `.sigstore.json`. The title is fixed: `Android {version} (versionCode {version_code})` |

## Fixed in code, not config

These stay in code for the standard flow (a ported flow keeps its app's own tables):

| Display type | Staged folder | Accepted sizes (the first is the fit target) |
|---|---|---|
| `APP_IPHONE_67` | `ios/6.7` | 1290×2796, 1284×2778 |
| `APP_IPAD_PRO_3GEN_129` | `ios/ipad-13` | 2064×2752, 2048×2732 |
| `APP_IPHONE_DUO` | `ios/iphone-duo` | 1398×2034, 2007×2853 |
| `WATCH` | `ios/watch` | 416×496 `APP_WATCH_SERIES_10`, 422×514 and 410×502 `APP_WATCH_ULTRA`, 396×484 `APP_WATCH_SERIES_7`, 368×448 `APP_WATCH_SERIES_4`, 312×390 `APP_WATCH_SERIES_3` |
| Play phone | `play/phone` | portrait, 9:16 ± 0.08, each side 320–3840; at most 8 staged |

Also fixed: the App Store version and creative state sets and timings, at most 10
screenshots per set, the build poll and wait ranges, 600 s upload clock skew, the JWT
lifetime (1200 s, refreshed after 600 s), HTTP timeouts, the transient status set, the PNG
checks, the Play track mapping, the release title format, the Apple WWDR G3 certificate
and bundletool 1.18.3 checksums, and the export options.
