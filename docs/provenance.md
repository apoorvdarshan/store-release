# Provenance

store-release v0.1.0 is Fud AI's release pipeline moved into a shared repository.
Source: [`apoorvdarshan/fud-ai`](https://github.com/apoorvdarshan/fud-ai) at
`a99591908d5f737405ca7251436d4669e32c90c6` (2026-10-20, PR #634; the same tree as
PR #633's merge `2c8d517d24a62d928f38ddb8cc275d74ba57468f` plus the store lock's
Pillow 13.0.0 bump). Both repositories are MIT-licensed by the same author.

Every behaviour, API call, switch, secret name, tag rule, title and asset name is
kept. Fud AI's constants became config values ([config.md](config.md)); module
boundaries are kept 1:1, so the ported tests changed only in imports and fixtures.

## File mapping

| fud-ai `a99591908` | store-release |
|---|---|
| `scripts/store/print_gates.py` | `store_release/switches.py` (plus the shared on/off value sets) |
| `scripts/release_notes.py` | `store_release/release_notes.py` (plus `--check-headings`) |
| `scripts/store/redact_credential_ids.py` | `store_release/redact.py` |
| `scripts/store/validate_catalog.py` (generic rules) and `scripts/store/sync_revenuecat_catalog.py` | `store_release/catalog.py`; the app-specific rules stay in the app as a `catalog.checks` hook |
| `scripts/store/stage_screenshots.py` | `store_release/screenshots.py` |
| `scripts/store/prepare_listing_text.py` | `store_release/text/listing.py` |
| `scripts/store/prepare_whats_new.py` | `store_release/text/whats_new.py` |
| `scripts/store/ios_release.py` | `store_release/ios/release.py` |
| `scripts/store/asc_release.py` | `store_release/ios/asc.py` (the hand-written App Store Connect client and store step, plus the read-only client) |
| bash steps of `.github/workflows/ios-release.yml` (select Xcode, SPM cache key, verify the archive, export options) | `store_release/ios/archive.py` |
| `scripts/store/android_release.py` | `store_release/android/release.py` (plus the read-only Play publisher) |
| `scripts/store/play_listing.py` | `store_release/android/play_listing.py` |
| `scripts/store/test_ios_release.py` (140 tests) | `tests/unit/test_ios_release.py` |
| `scripts/store/test_android_release.py` (54 tests) | `tests/unit/test_android_release.py` |
| `scripts/store/test_stage_screenshots.py` (21 tests) | `tests/unit/test_stage_screenshots.py` |
| `requirements-store.in` / `requirements-store.txt` | `requirements.in` / `requirements.txt` (verbatim) |
| `store/gates.env.example` | `gates.env.example` (app-neutral wording) |
| `ios-release.yml` jobs `prepare`, `build`, `wait-for-build`, `app-store-connect`, `github-release` | `ios-prepare.yml`, `ios-build.yml`, `ios-wait-for-build.yml`, `ios-app-store-connect.yml`, `ios-github-release.yml` |
| `android-release.yml` jobs `gates`, `release` | `android-gates.yml`, `android-publish.yml` |
| `store-automation.yml` job `dry-run` | `checks.yml` |
| `store/README.md` (switches, secrets, the CI signing identity, re-runs) | [switches.md](switches.md), [secrets.md](secrets.md), [rerun.md](rerun.md) |

New in store-release: `config.py`, `context.py`, `paths.py`, `stage.py`, `callers.py`,
`hooks.py`, `github.py`, `yanked.py`, `selftest.py`, `smoke.py`, `canary.py`,
`ios/plan.py`, `android/plan.py`, `ios-store-plan.yml`, `android-store-plan.yml` and
the `job-setup` action.

`tests/unit/PORTED_FROM_FUD_AI.txt` lists the 215 original test ids;
`test_port_inventory` fails unless every one exists and none is skipped.

## Parity

`tests/parity/upstream_fud_ai_a99591908/` holds verbatim copies of the twelve fud-ai
scripts above, taken with `git show a99591908d5f737405ca7251436d4669e32c90c6:<path>`
and hashed in `MANIFEST.sha256`; they are never edited. The parity tests run the
upstream script and the package on identical inputs and compare outputs, files and
exit codes. The only differences they allow are D1, D7, D12, D14 and D16 below (the
ones visible at the script level), each with its own test
(`tests/parity/test_parity_allowlist.py`).

## Behaviour deltas from fud-ai

Everything not listed here behaves exactly as fud-ai did.

| # | Delta | Effect on Fud AI |
|---|---|---|
| D1 | Play: each padded frame's size is validated after padding | none (padding always yields a valid size, 1290×2796 → 1573×2796) |
| D2 | Android master off on a tag: the caller skips the gate jobs and the release, a green no-op | none |
| D3 | GitHub Release `--latest` only when the tag's version is the highest among non-draft releases with the same tag prefix, else `--latest=false` (iOS; Android production). Android non-production keeps `--prerelease --latest=false` | re-running an older tag's release step no longer takes Latest |
| D4 | The SPM cache key includes the Xcode version and build and a digest of `developer_dir` | one cold SPM cache after migration, then a new cache when Xcode changes |
| D5 | Validation dispatches gain the read-only `store-plan` job | an extra job on dispatches; nothing is written |
| D6 | Explicit secret mapping, with presence checks at the start of each secret-using job | a missing secret fails earlier, by name |
| D7 | Listing, What's New and screenshot staging go to `RUNNER_TEMP`, never into the app checkout | none (the same bytes and file names are uploaded) |
| D8 | `actions/setup-python` 3.13 in every job, including the macOS build job | none (the build job's tooling stays stdlib-only) |
| D9 | Runtime caller check in prepare/gates (pins and concurrency) | fails only for a hand-edited caller |
| D10 | Yanked versions: a warning in prepare/gates, a failure in PR checks | none until a version is yanked |
| D11 | App hooks run with a scrubbed environment; the ASC key and issuer IDs are set only on the steps that need them | none |
| D12 | The Play re-run detector also matches the job name `release / release` | needed inside a called workflow |
| D13 | Check-run names become `<caller job> / <inner job>`; the PR workflow is `Store checks` (`checks / checks` plus `store-release`) | none required |
| D14 | `STORE_SYNC_REVENUECAT` is parsed strictly (an invalid value fails); fud-ai read it loosely as off | none while it is unset |
| D15 | PR checks add config, callers, pins, yanked, duplicate release-note headings, Pillow staging, project export compliance, project version and gradle parsing on the real files | PR-only |
| D16 | Messages that named a code constant name the config key | wording only |
| D17 | Preflight, build number and build wait use the read-only client | none (the same GETs) |
| D18 | The Sigstore and attestation signer is `apoorvdarshan/store-release/.github/workflows/android-publish.yml@<sha>`; the repository extension is still the app's | verification commands change ([verification.md](verification.md)) |
| D19 | Temp names come from `app.slug` (`fudai.xcarchive`, was `FudAI.xcarchive`) | none |

## Deliberately unchanged

The in-app purchase route (`POST /inAppPurchaseSubmissions` after the final recheck),
screenshot comparison by `(fileName, fileSize, MD5)`, no What's New repeat guard,
screenshots validated only in the store job, all-configuration project version reads,
the draft rule, catch-up only in `WAITING_FOR_REVIEW` (`IN_REVIEW` refuses), the
silent cap at 8 Play frames, the RevenueCat gate's position, the
`r0adkll/upload-google-play` inputs, release titles and asset names, the export
options and the double use of `exportArchive`. Per-app differences arrive one app per
minor release ([roadmap.md](roadmap.md)).
