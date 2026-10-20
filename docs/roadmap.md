# Roadmap

v0.1.0 is Fud AI's pipeline. v0.2.0 runs the other five apps' pipelines too, in one
release, as ported flows ([flows.md](flows.md)): each app's scripts moved verbatim and
selected by `ios.flow` / `android.flow`, with the shared workflow steps driven by new
optional config keys whose defaults keep Fud AI's behaviour. Each app moves over in
its own pull request after a clean validation dispatch ([examples/MIGRATING.md](../examples/MIGRATING.md)).

The table below was the earlier one-app-per-release plan. Its config hooks are what
the flows now carry inside them; turning them into shared keys is the unification
work that follows:

| Release | App | Config hooks to add | App-side and ruleset items (Apoorv approves the marked ones) | Also handled |
|---|---|---|---|---|
| v0.2.0 | Scowld | `ios.listing.source = "files"` (`dir`, `app_info`, `required`, `privacy_choices_url`); `ios.whats_new.source = "files"`; `ios.whats_new.repeat_guard`; the `screenshots.json` manifest layout (or renaming into ordered set folders with unchanged MD5s; needs Apoorv's OK); `ios.screenshots.rgb = "copy-verbatim"`; `ios.screenshots.compare = "size-md5"`; `ios.products.submit = false`, `pending_outside_catalog = "refuse"`; `ios.review.empty_drafts = "fail"`; title placeholder `{tag}`; `ios.github_release.source_archive` (git archive, `.sha256`, cosign, provenance); `github_release.on_existing = "upload-missing"`; the union redactor (UTF-16BE, file names); `ios export-listing` (GET-only) | gate job `tests`; `check_xcresult.py` stays, so `Analyze (python)` is untouched; the cosign identity docs pin `--certificate-github-workflow-repository apoorvdarshan/scowld` | PR-check semantics per part; verification identity pinned to the repository |
| v0.3.0 | PCM Notes | `ios.build.post_archive` (`check_offline_notes.sh`); `ios.gate_policy = "when-uploading"` (switching to parallel gates needs Apoorv's OK); `ios.products.iap_route = "review-item"`; `required = false` for non-WATCH sets; the late What's New repeat guard; title `{tag}{build_suffix}`; `on_existing = "keep"` | catalog cross-checks become the app's `scripts/ci/check_catalog.py`; its NOTICE is all rights reserved, so the canary never uploads; suggest a `v*` tag ruleset | the review-item route has never run with a pending product: a clean store-plan plus a read-only check that Apple accepts consumables and tips as review items before submitting with pending products; suggest `IOS_PUBLIC_RELEASE=false` for its first shared release (Apoorv's call) |
| v0.4.0 | ABYSM | per-set `accepted` sizes, including landscape; `ios.strict_app_id`; `ios.review.compare_only_states` (+`IN_REVIEW`); `ios.creative.require_image = false`; `ios.products.catch_up = "refuse"` (catch-up only for products approved before); fuzz targets move in (Atheris) | `scripts/ci/test_test_ios.py` stays in the app; OpenSSF Scorecard fuzzing and Best Practices answers updated truthfully | catch-up of first-time products |
| v0.5.0 | Quit All | `ios.build.extensions`, `app_groups`, `verify_ipa` (the double export); manifest with `APP_IPHONE_65`; dual compare (committed or staged bytes); multi-locale files without App Info; `ios.whats_new.version_file`; `iap_route = "review-item"`; compare-only states; `require_image = false`; title `{tag} (build {build})` | removing `Analyze (python)` from its ruleset only if no Python remains, with the unattributed-changes approval set explicitly (Apoorv's approval), or keep a real Python file | its What's New PR semantics (`version_file`) |
| v0.6.0 | Verceltics iOS | `ios.whats_new.source = "release-notes"` (`plain_text`); listing files plus App Info, failing when it differs and nothing is editable; `fit = false`; `ios.uses_non_exempt_encryption` in config, checked at submission; `on_existing = "fail"`; gate job `catalog` through a `ci.yml` input, on dispatches too | `scripts/test.sh` discovery; keep the coverage of the required `catalog` check (Apoorv's approval) | |
| v0.7.0 | Verceltics Android | `android.build.signing = "gradle-properties"` (properties, alias); `sdk_packages`; `production_access = false` note; release-notes What's New; files listing; `pad_to_9_16 = false` with RGB flattening; asset name with `{version_code}`; keep its Play-state re-run model unless Apoorv approves Fud AI's | a first real internal-track test before any production switch | |

Later, not in any of these:

- Unifying the six flows into one engine with per-app keys (the table above), one app
  at a time, with that app's suite and inventory as the safety net.
- ABYSM's and PCM Notes' Atheris fuzz targets for the store scripts.

- OpenSSF Scorecard and fuzzing for store-release itself (fuzz targets for the flows).
- Splitting Sigstore signing out of the Gradle job, so app code never holds
  `id-token: write`.
- Uploading the verified IPA instead of exporting it twice.
- `store-release` as a required check per app (drop the `checks.paths` filter first).
- App tag rulesets (suggestion only).
- Whether Sesslet, the remaining consumer of the shared CI signing identity, also moves.
