# `store/store-release.toml` (schema 1)

Each app keeps one file, `store/store-release.toml`, that tells store-release where
its store files are and what its fixed identities are (bundle id, package, team,
upload certificate). Switches stay in repository variables ([switches.md](switches.md)),
secrets in repository secrets ([secrets.md](secrets.md)). Nothing in the file is secret.

Worked examples: [Fud AI](../examples/fud-ai/store-release.toml) and a
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
| `catalog.path` | path | required in the table | the product catalog JSON |
| `catalog.groups` | list[str] | required | non-empty, unique, `^[A-Za-z_]+$`; each must be a list in the catalog. Only these ids are ever attached to App Review |
| `catalog.id_prefix` | str | required | every App Store product id starts with it |
| `catalog.play_rules` | bool | `false` | Play rules: a `play` block if and only if `android` is in `platforms`; id and base-plan formats; `revenuecat_product_id == "<sub>:<base>"` for the `subscriptions` group; Play ids unique |
| `catalog.revenuecat` | path | none | when set, `revenuecat-gate` also counts its `entitlements` and `offerings` |
| `catalog.checks` | list[path] | `[]` | app hook scripts (`.py`/`.sh`) run after the generic checks wherever `catalog check` runs |
| `checks.paths` | list[str] | `[]` | path filter of the app's `store-checks.yml` for `push` and `pull_request`; `[]` = no filter |
| `ios.tag_prefix` | str | required | `^([a-z]+-)*v$`; tags are `<tag_prefix><MARKETING_VERSION>` |
| `ios.bundle_id` | str | required | reverse-DNS; must resolve to exactly one App Store Connect app, and the archive's main bundle must carry it |
| `ios.asc_app_id` | str | required | `^[0-9]{6,12}$`; version creation refuses any other app |
| `ios.team_id` | str | required | `^[A-Z0-9]{10}$`; ExportOptions `teamID` |
| `ios.project` | path | required | the `*.xcodeproj`; its `project.pbxproj` holds `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION`, identical in every configuration |
| `ios.scheme` | str | required | the scheme to archive |
| `ios.app_target` | str | required | the target whose configurations decide export compliance |
| `ios.info_plist` | path | required | the app target's Info.plist; `config check --files` checks it is the target's resolved `INFOPLIST_FILE` |
| `ios.locale` | str | `en-US` | the listing, What's New and creative locale |
| `ios.gate_jobs` | array of tables | required | non-empty; each `{ id = "<job id>", uses = "./.github/workflows/<file>.yml" }`, ids unique, `^[a-z0-9][a-z0-9_-]*$`, not a store-release job name, `web`, `android`, `store-release` or `checks`. The app's own test workflows that must pass on a tag before anything after prepare runs; rendered as `if: github.event_name == 'push'` jobs with `contents: read` |
| `ios.build.developer_dir` | abs path | required | the Xcode to use; must not contain `beta` (any case) |
| `ios.build.checkout` | list[pattern] | required | the build job's sparse checkout (the config and the hook scripts are added automatically) |
| `ios.build.package_cache_files` | list[glob] | `["**/Package.resolved"]` | files hashed into the SPM-sources cache key |
| `ios.build.pre_archive` | list[path] | `[]` | app scripts run before signing material is installed, with a scrubbed environment plus `MARKETING_VERSION`, `BUILD_NUMBER`, `DEVELOPER_DIR` |
| `ios.listing.source` | enum | `markdown` | *hook*; `markdown`: the first fence under each fixed heading (`App Name`, `Subtitle (30 chars max)`, `Promotional Text (170 chars max)`, `Keywords (100 chars max)`, `Description`, `Privacy URL`, `Support URL`, `Marketing URL`); uploads description, keywords, promotional text, marketing and support URLs |
| `ios.listing.markdown` | path | `APPSTORE.md` | |
| `ios.whats_new.source` | enum | `markdown` | *hook*; the first fence under the first `## ` line containing `heading`; at most 4,000 characters, plain text |
| `ios.whats_new.markdown` | path | `ios.listing.markdown` | |
| `ios.whats_new.heading` | str | `What's New` | substring match |
| `ios.screenshots.sets` | array of tables | none | upload order; each has `display_type` (`APP_IPHONE_67`, `APP_IPAD_PRO_3GEN_129`, `APP_IPHONE_DUO`, `WATCH`), `dir` and `required`. Non-WATCH sets must have `required = true` (a missing or empty folder fails); the WATCH set must have `required = false` (staged only when its folder exists) and come last; display types are unique. Frames are numbered `NN-name.png` |
| `ios.creative.dir` | path | `store/creative/ios` | `header/` takes one 3840×1646 PNG; `search-results/` one 3:2 PNG or JPEG, 1920–3840 wide; no alpha |
| `ios.products.iap_route` | enum | `submission` | *hook*; subscriptions as `subscriptionVersion` draft items, READY_TO_SUBMIT in-app purchases via `POST /inAppPurchaseSubmissions` after the final build recheck. The table itself is what `IOS_SUBMIT_REVIEW` needs |
| `ios.github_release.title` | template | `iOS {version} (build {build})` | `{version}`; `{build}` is the uploaded build, else the project's `CURRENT_PROJECT_VERSION` |
| `android.tag_prefix` | str | required | `^([a-z]+-)*v$`; the tag must equal `<prefix><versionName>` |
| `android.package` | str | required | the applicationId and Play `packageName` |
| `android.gradle_file` | path | `android/app/build.gradle.kts` | exactly one `versionName = "X"` and one `versionCode = N` line |
| `android.upload_cert_sha256` | str | required | `^[0-9a-f]{64}$`; the only allowed signer of the AAB and APK |
| `android.locale` | str | `en-US` | listing locale; the What's New file kept for upload is `whatsnew-<locale>` |
| `android.gate_jobs` | array of tables | required | as for iOS; rendered with `needs: gates` and `if: needs.gates.outputs.mode == 'release' && needs.gates.outputs.build == 'true'` |
| `android.build.dir` | path | `android` | the Gradle working directory |
| `android.build.tasks` | list[str] | `[":app:bundleRelease", ":app:assembleRelease"]` | each `^:?[A-Za-z0-9_.:-]+$`; always run with `--no-build-cache` |
| `android.build.aab` | path | `<dir>/app/build/outputs/bundle/release/app-release.aab` | |
| `android.build.apk` | path | `<dir>/app/build/outputs/apk/release/app-release.apk` | |
| `android.build.checkout` | list[pattern] | required | the release job's sparse checkout |
| `android.build.signing` | enum | required | *hook*; `keystore-properties`: the four keystore secrets become `storeFile`, `storePassword`, `keyAlias`, `keyPassword` (Java-properties escaped) in `RUNNER_TEMP/<slug>-keystore.properties`, mode 600, linked at `keystore_properties` |
| `android.build.keystore_properties` | path | `<dir>/keystore.properties` | where Gradle reads the signing config; the app must gitignore it |
| `android.listing.source` / `.markdown` | enum / path | `markdown` / `PLAYSTORE.md` | *hook*; headings `1. App Name`, `2. Short Description`, `3. Full Description`; at most 30/80/4,000 characters |
| `android.whats_new.source` / `.markdown` / `.heading` | enum / path / str | `markdown` / `PLAYSTORE.md` / `What's New` | *hook*; `<locale>...</locale>` blocks of at most 500 characters, plain text |
| `android.screenshots.dir` | path | required in the table | numbered `NN-name.png` phone frames |
| `android.screenshots.pad_to_9_16` | bool | `true` | *hook*; only `true`: off-aspect frames are padded onto black at 9:16, then validated |
| `android.github_release.asset_name` | template | `{slug}-v{version}` | `{slug}`, `{version}`, `{version_code}`; must render to `^[A-Za-z0-9._-]+$`; names the `.aab`, `.apk` and their `.sigstore.json`. The title is fixed: `Android {version} (versionCode {version_code})` |

## Fixed in code, not config

These stay in code in v0.1.0 (they are the same for every app today):

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
