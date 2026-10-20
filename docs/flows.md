# Release flows

**New apps use the standard flow**, the default: store-release's own modules
(`store_release/ios`, `store_release/android`), configured by
[`store-release.toml`](config.md). Nothing on this page is needed to set one up.

The **ported flows** exist for five apps that had grown their own release scripts before
they moved to store-release (Scowld, Verceltics, ABYSM, PCM Notes and Quit All), each
different in dozens of details: which version states allow what, how screenshots are
compared and replaced, which review draft a version goes into, how products reach App
Review, when What's New is checked against the live version. v0.2.0 moved every one of
them here unchanged, selected with `ios.flow` (and, for Verceltics, `android.flow`), so
that no app's behaviour changed when it moved. They stay as they are until they are folded
into the standard flow ([roadmap](roadmap.md)); don't pick one for a new app. The standard
flow was called `fud-ai` until v0.3.0, after the app it came from
([provenance](provenance.md)).

## What a ported flow is

`store_release/flows/<package>/` holds the app's own scripts at the commit
[provenance.md](provenance.md) names. The only edits are the port's
(`store_release/flows/port.py`), and `tests/unit/test_flows.py` proves every module is
exactly that port of its verbatim copy in `tests/parity/upstream_<package>_<sha9>/`:

- `ROOT = Path(__file__).resolve().parents[N]` becomes a placeholder that
  `configure()` replaces with the app checkout;
- `sys.path` lines go and sibling imports become relative (`from . import ios_metadata`);
- the app's identity literals (bundle id, App Store Connect app id, tag prefix, locale,
  Play package, upload certificate) become placeholders that `configure()` sets from
  `store/store-release.toml`;
- a module constant named in `RENAMES` gets a new name with the same value and uses
  (Verceltics' `BILLING_MODELS` is `PRODUCTS_SOURCE`, which CodeQL's clear-text
  logging query does not read as billing data);
- an import those edits leave unused is removed.

`configure()` (each flow's `__init__.py`) sets those values and every path the config
names (listing folder, screenshot folders or manifest, creative folder, catalog, StoreKit
file, product source), then recomputes everything the module derived from them
(`store_release/flows/rebind.py`). `tests/unit/test_flows.py` checks that, configured
from `examples/<app>/store-release.toml`, every module constant equals the app's own
script's, and nothing still holds a placeholder.

Each app's own store test suite (900 tests across the five) runs unchanged against its
ported flow in `test (strict)` (`tests/parity/test_parity_flows.py`). The few that read
the app's committed files, test the app's workflow files or test a helper store-release
replaces are listed, each with its reason, in `tests/flows/<package>.inventory`.

## The commands a flow answers

The reusable workflows run the same `run.py` commands for every flow; the flow decides
what they do, exactly as that app's own `ios-release.yml` did
(`store_release/flows/commands.py`):

| Command | What the ported flow runs |
|---|---|
| `ios gates`, `ios build-number`, `ios wait-build ...` | the app's `ios_release.py` |
| `ios check-inputs` (prepare, offline) | the app's local file check for the enabled switches: Scowld `asc_release.py --dry-run` (file switches only); Verceltics `ios_metadata.py check` / `whats-new --tag` / the creative dry-run; ABYSM `--dry-run --build-id DRY-RUN --build-number 1` on every run; PCM Notes `--dry-run --build-id pending --build-number pending`; Quit All `--dry-run --version V --build-id CHECK --build-number 0` |
| `ios store-step --create-version --version V` | the app's `asc_release.py --create-version` |
| `ios preflight --version V` (prepare, read-only) | Scowld, ABYSM, Quit All: `asc_release.py --preflight`; Verceltics: `ios_metadata.py prepare` then `asc_release.py --preflight --metadata-dir`; PCM Notes: `ios_release.py preflight` with its flags |
| `ios store-step --version V [--build-id I --build-number N]` | the app's store step (Verceltics prepares its metadata folder first) |
| `ios export-compliance --info-plist P` | the archived app's answer: Scowld, ABYSM, PCM Notes run their own command; Verceltics and Quit All checked the plist in their workflow (a boolean; YES needs a compliance code) |
| `ios export-listing ...` | the app's GET-only listing exporter (Scowld, Verceltics, ABYSM, PCM Notes) |
| `ios pr-checks` (Store checks) | the app's pull request dry-runs with every switch on |
| `catalog check [--platform] [--event]` | the app's catalog validator, when its prepare ran it (Verceltics and ABYSM on every run, PCM Notes and Quit All on tag runs; every pull request) |
| `ios plan` / `android plan` (validation dispatches) | the read-only store plan: the switches a tag run would resolve, then the flow's own read-only checks through a client that refuses every write. iOS: the catalog check and the local files a tag run's prepare checks, then the preflight; when the App Store version does not exist yet and `IOS_CREATE_VERSION` would create it, every check that needs no version instead (What's New against the live version for Scowld and PCM Notes, Quit All's What's New version file with the local files, Scowld's pending products, ABYSM's catalog review items, the review drafts), and a list of what only the created version can show. PCM Notes' store step's What's New check also runs for an existing version |
| `android gates`, `check-version-code`, `verify`, `play-state`, `check-tracks`, `play-listing ...`, `pr-checks` | Verceltics' `android_release.py` and `play_listing.py` |

The standard flow keeps its own commands (`version-preflight`, `listing prepare`,
`whats-new prepare`, `screenshots stage`); a ported flow refuses them, and the standard flow
refuses the ported flows' commands.

## Per app

Each list is what the app's pipeline did and still does. The config keys that are not
flow-internal are in [config.md](config.md).

### Scowld (`ios.flow = "scowld"`)

- iOS only (no `[android]`), tags `v<version>`, gate job `tests`, gates run next to
  prepare (`ios.gate_policy = "parallel"`).
- Listing and What's New from `store/metadata/ios/<locale>/` (every locale folder),
  including App Info (name, subtitle, privacy URL); an empty or missing file leaves its
  field unchanged except `description`; What's New must be plain text and differ from
  the newest live version's (checked in the preflight and again before writing).
- Screenshots listed per locale in `screenshots.json`; RGB files and JPEGs are uploaded
  byte for byte, PNGs that store alpha are flattened over black; a set is replaced whole
  when its ordered MD5 list differs.
- Every locked field is checked before anything is written; in WAITING_FOR_REVIEW
  everything is compare-only.
- In-app products are never submitted; review submission stops when any product is
  READY_TO_SUBMIT, DEVELOPER_REJECTED or READY_FOR_REVIEW. More than one empty review
  draft is ambiguous and fails.
- GitHub Release "Scowld X.Y.Z", always Latest, with the signed source archive
  (`ios.github_release.source_archive`); an existing release keeps its notes and gets the
  three archive files again unless all are attached (`on_existing = "upload-missing"`).

### Verceltics (`ios.flow` and `android.flow = "verceltics"`)

- Tags `ios-v` / `android-v`; iOS gate job `tests`; Android gate job `quality`
  (`ci.yml` with `android-release: true`, rendered from `android.gate_jobs[].with`).
- Every iOS prepare runs the app's offline repository tests (`ios.prepare.checks`) and
  the iOS catalog check against `Products.storekit`; the Android release job checks the
  catalog against `BillingModels.kt`.
- iOS listing from `store/metadata/ios/<locale>/` with App Info (unknown files fail;
  `name`, `description`, `support_url` required); What's New from the tag's
  RELEASE_NOTES.md section converted to plain text, on both platforms.
- Screenshots from `store/app-store/screenshots/{iphone,ipad,iphone-duo,watch}`, every
  frame re-encoded as RGB, never resized; a set is replaced incrementally.
- Subscriptions are attached to the version's draft; catalog in-app purchases go to
  review through `POST /inAppPurchaseSubmissions` after the final build recheck; an
  unanswered build's export compliance is answered from the catalog, and a mismatch stops.
- GitHub Release "iOS X.Y (build N)", create only (`on_existing = "fail"`), always Latest.
- Android: signing with the keystore and a password file passed as Gradle properties
  (`android.build.signing = "gradle-properties"`, alias `upload`); SDK packages
  `platform-tools` and `platforms;android-37.0`; Play listing from
  `store/metadata/play/`, What's New from the release notes, screenshots uploaded as
  they are (`pad_to_9_16 = false`); every release run checks the versionCode is new
  with a read-only Play edit; re-runs adopt an upload every selected track already
  holds (`play-state`); the Play key is written to `RUNNER_TEMP` for the upload action;
  production releases are always Latest.

### ABYSM (`ios.flow = "abysm"`)

- Tags `v`, gate job `quality`; the offline check (`ios check-inputs`) and the catalog
  check run on every run.
- Listing, App Info and What's New from files, plus `privacy_choices_url.txt`;
  `description` and `keywords` required; URLs must be `https://`.
- Landscape and portrait screenshot sizes, every frame re-encoded as RGB; a set is up
  to date only when its checksums match and every screenshot is COMPLETE; WAITING_FOR_
  REVIEW and IN_REVIEW are compare-only.
- The App Store Connect app id is checked on every lookup; the preflight uses a client
  that raises on any write.
- Catalog products ride the version's draft as review submission items and go out with
  one submit; a product left behind is never sent to review on its own.
- GitHub Release "ABYSM vX.Y" plus " (build N)" when this run uploaded one; an existing
  release is left as it is (`on_existing = "keep"`).

### PCM Notes (`ios.flow = "pcm-notes"`)

- Tags `v`, gate job `tests`, which runs after prepare and only on a tag run that
  uploads a build (`ios.gate_policy = "when-uploading"`); a listing-only tag run does
  not wait for it.
- The build checkout includes the note folders, and after the archive check the app's
  `scripts/ci/check_offline_notes.sh` (`ios.build.post_archive`) compares the bundled
  pages with git.
- Listing and App Info from files across every locale; What's New must differ from the
  live version's, checked in the store step.
- Screenshots from `store/metadata/screenshots/ios/{6.7,12.9,iphone-duo,watch}`; a set is
  up to date when its checksums match the committed files or their RGB copies.
- Catalog tips and consumables go out as `inAppPurchaseVersion` review items with the
  version; the catalog must match `TipStore.swift` (in order) and `PCMNotes.storekit`.
- GitHub Release "PCM Notes vX.Y" plus " (build N)" when uploaded; an existing release
  is left as it is.

### Quit All (`ios.flow = "quit-all"`)

- iPhone only, tags `v`, gate job `tests` after prepare on every tag push
  (`ios.gate_policy = "after-prepare"`).
- The archive and the App Store-signed IPA must hold exactly the app and its widget,
  each signed for the team with the shared app group (`ios.build.extensions`,
  `ios.build.app_groups`, `ios.build.verify_ipa`: the build exports once to verify, and a
  release run exports again to upload).
- Listing from every locale folder without App Info; `whats_new_version.txt` must name
  the release's version (`ios.whats_new.version_file`).
- Screenshots listed in `screenshots.json` (including `APP_IPHONE_65`); a set is up to
  date when its checksums match the RGB copies or the committed files and none FAILED.
- Products go out as review items with the version; the catalog must match
  `PaywallManager.swift` and `Products.storekit` (`catalog check`).
- GitHub Release "Quit All vX.Y (build N)" (the project's build number without an
  upload), create only.

## Changing a flow

A fix to a ported flow is a store-release change like any other. To bring in a newer
version of an app's scripts, bump its commit in `store_release/flows/port.py` and run
`scripts/port_flows.py` with the app checkouts; the port and constant tests then show
exactly what moved. Merging flows into one engine is later work: the per-app suites and
the inventories are the safety net it needs.
