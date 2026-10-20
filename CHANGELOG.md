# Changelog

All notable changes to store-release. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) ([release process](docs/release-process.md)). Every
release section has an `Impact:` line: `checks-only`, `validation`, `release-path`
or `config-breaking`.

## [Unreleased]

Impact: release-path

### Added

- `run.py init`: scaffolds `store/store-release.toml`, the three caller workflows pinned to a
  release (the `vX.Y.Z` tag's commit, looked up on GitHub, or `--sha`) and
  `.github/dependabot.yml` for a new app, from flags or, in a terminal, answers. The config is
  validated before anything is written, a bad answer names its flag, and nothing is
  overwritten without `--force`.
- `examples/templates/` (`ios-only`, `android-only`, `ios-and-android`): what `init` writes
  for placeholder values; a test and `scripts/render_examples.py --check` keep them equal.
- `ios.build.runner` (optional): the build job's runner label, passed to `ios-build.yml`
  only when set, so every existing caller renders as before.

### Changed

- The default flow is called `standard`; `fud-ai`, its old name, is still accepted in
  `ios.flow` and `android.flow` and means the same flow. Messages and
  `STORE_RELEASE_IOS_FLOW` / `STORE_RELEASE_ANDROID_FLOW` say `standard`.
- Private app repositories work: job-setup fetches a job's files with the job's read-only
  token, given only to that git command as environment config (never `.git/config` or
  argv). Public repositories fetch the same files as before.
- `ios.info_plist` is optional for the standard flow too, for an app whose Info.plist Xcode
  generates entirely; `config check --files` then checks the project sets no
  `INFOPLIST_FILE`.
- Store checks (standard flow) prepare and dry-run only the listing, What's New,
  screenshot, creative and review steps whose config table exists, so an app without them
  passes. `listing prepare`, `whats-new prepare` and `screenshots stage` with
  `--platform all` skip a platform without its table; an explicit platform is unchanged.
  An app with every table (Fud AI) runs exactly the same steps.
- No app's identity appears outside `store_release/flows/`: a test now checks the six
  apps' names, bundle ids, App Store Connect ids, team, packages, upload certificates and
  product prefixes, and any account or e-mail address, in the package, the reusable
  workflows and job-setup. `canary --expect` is required (store-release's CI passes it).

### Fixed

- The ported flows' iOS store plan reported CLEAN when the App Store version did not exist
  yet and `IOS_CREATE_VERSION` would create it, without running any later check: a tag
  whose What's New was still the live version's would create the version and then fail.
  The plan now runs every check that needs no version, as a tag run makes them: the
  catalog check and the local files (including Quit All's What's New version file), What's
  New against the live version (Scowld, PCM Notes), pending in-app products (Scowld), the
  catalog review items (ABYSM) and the review drafts. It lists, in a line and a notice,
  what only the created version can show. For an existing version it also runs the catalog
  and local-file checks before the preflight, and PCM Notes' store-step What's New check.
- The iOS caller's `marketing_version` hint says `e.g. 1.2` instead of an app's version.
  `caller check --pr` ignores the Run workflow inputs' descriptions, like comments, so
  callers rendered by v0.2.0 keep passing.

### App actions required

None. Existing configs and callers keep working unchanged: the rendered callers differ
only in a comment (Android) and an input description (iOS), which `caller check --pr`
ignores. Re-render them to pick up the new wording.

## [0.2.0] - 2026-10-21

Impact: release-path

The other five apps' pipelines: Scowld, Verceltics (iOS and Android), ABYSM, PCM
Notes and Quit All, each kept as it is ([flows](docs/flows.md)). Fud AI's behaviour,
config and rendered callers do not change.

### Added

- Ported flows (`store_release/flows/`), selected by `ios.flow` and `android.flow`
  (default `fud-ai`): each app's store scripts at the commit in
  [provenance](docs/provenance.md), with only the port's edits, proven by
  `tests/unit/test_flows.py`; the apps' own 900 store tests run unchanged against
  them (`tests/parity/test_parity_flows.py`), with the 101 left out listed with
  reasons in `tests/flows/*.inventory`.
- Config keys (all optional, defaults keep Fud AI's behaviour): `ios.gate_policy`,
  `ios.prepare.checks`/`checkout`, `ios.build.post_archive`/`extensions`/`app_groups`/
  `verify_ipa`, `ios.listing.dir`, `ios.whats_new.version_file`,
  `ios.screenshot_manifest`, `ios.catalog.storekit`/`products_source`,
  `ios.github_release.on_existing`/`latest`/`source_archive` and the title
  placeholders `{tag}`, `{build_suffix}`; `android.build.signing =
  "gradle-properties"` with its property names, `key_alias`, `sdk_packages`;
  release-notes What's New and file listings for Android; gate jobs' `with:` inputs;
  `unique_headings`.
- `ios-source-archive.yml` (the eleventh reusable workflow), the
  `keystore_only` input of `android-publish.yml`, `ios verify-bundles`, the
  `pre_archive`/`post_archive`/`prepare` hook stages, file names in the redactor.
- `examples/{scowld,verceltics,abysm,pcm-notes,quit-all}/` with rendered callers and
  [MIGRATING.md](examples/MIGRATING.md); CI lints every example's callers and the
  workflows in its `app-files/`.
- `examples/abysm/app-files/`: ABYSM's tests of `scripts/test-ios.sh`, moved unchanged
  from its store tests to `scripts/ci/test_test_ios.py`, and `test-ios-script.yml`,
  which runs them on pull requests that touch the script.
- `ios pr-checks` for ABYSM checks the committed project's export compliance first, as
  its pull request tests did; its config names `ios.info_plist`, so prepare and the store
  checks check that Info.plist out.
- Atheris fuzz targets (`fuzz/`) for the config loader, the PNG checks, the redactor,
  release notes and the switches, run by `fuzz.yml`.
- store-release's own releases ship the commit's source archive with its SHA-256, a
  Sigstore keyless signature bundle and a SLSA build provenance attestation
  (`release.yml`; verify commands in its header).

### Fixed

- certifi 2026.10.19 in `requirements.txt` (Dependabot alert #1: it shipped emSign root
  certificates whose intermediate keys were compromised).

### App actions required

None for Fud AI, which stays on v0.1.0 until it bumps its pin. Each of the other apps
moves in its own pull request ([examples/MIGRATING.md](examples/MIGRATING.md)); until
then it keeps its own pipeline. No switch, secret or variable changes.

### Verification

- CI on `main` at `3df2938902969b1ef2b2dc5d865c490fdbc98c02`, the code this release
  tags (the release commit changes only this file, `__version__`, the version in the
  README, `docs/plan.md` and the examples' rendered comments, and two tests that now
  read `__version__`):
  [CI 39505133218](https://github.com/apoorvdarshan/store-release/actions/runs/39505133218)
  (`test (stdlib)`, `test (strict)`, `test (macos)`, `lint`, `workflows`, and the
  advisory `canary (fud-ai)`) and
  [CodeQL 39505133135](https://github.com/apoorvdarshan/store-release/actions/runs/39505133135)
  (`Analyze (python)`, `Analyze (actions)`). `release.yml` checks the required ones
  again on the release commit.
- The five apps' prepare gates and store checks ran on each app's real tree at its flow
  commit, cut down to each job's sparse checkout (PR #16, which fixed ABYSM's).
- Re-run probe: not run again. `probe-rerun.yml` and `probe-inner.yml` are unchanged
  since the v0.1.0 results ([docs/rerun.md](docs/rerun.md#results)), and the new jobs
  are called the same way.
- No app validation dispatches yet: they are step 4 of each app's migration pull request
  ([examples/MIGRATING.md](examples/MIGRATING.md)). A defect they find ships as v0.2.1
  (tags never move).

## [0.1.0] - 2026-10-20

Impact: release-path

The first release: Fud AI's release pipeline, moved into a shared repository with its
behaviour kept ([provenance](docs/provenance.md)).

### Added

- The `store_release` package: Fud AI's store tooling (fud-ai
  `a99591908d5f737405ca7251436d4669e32c90c6`) with its constants moved into
  `store/store-release.toml` (schema 1), plus config validation, per-job sparse
  checkout paths, job-boundary checks, caller rendering and checks, yanked versions,
  the hand-written App Store Connect client and a read-only store plan.
- Ten reusable workflows (`ios-prepare`, `ios-build`, `ios-wait-for-build`,
  `ios-app-store-connect`, `ios-github-release`, `ios-store-plan`, `android-gates`,
  `android-publish`, `android-store-plan`, `checks`) and the `job-setup` action.
- The 215 ported fud-ai store tests, new unit tests, fixture-app tests, a
  differential parity suite against the vendored fud-ai scripts, workflow tests,
  and docs tests.
- Fud AI's config and rendered callers in `examples/fud-ai/`.
- Pins on an app's own files, `ios.export_compliance` and `ios.creative.sizes`,
  checked by `config check --files` (Store checks) and `config check --pinned`
  (every iOS prepare): fud-ai's unit tests pinned these facts in its real files, and
  Fud AI's config pins them (export compliance `NO`; the two Asset Library images).
- `revenuecat-gate --check-switch` in Android gates: an invalid
  `STORE_SYNC_REVENUECAT` fails a tag run before anything is built.
- Store checks run each platform's steps only when the config has that platform
  (`STORE_RELEASE_HAS_IOS`, `STORE_RELEASE_HAS_ANDROID`), so single-platform apps pass.
- Pin checks against the release tag: `pins check` and `caller check --pr` (Store
  checks) fail unless store-release's `vX.Y.Z` tag points at the pinned commit,
  `caller check --runtime` (prepare, gates) warns, and `callers render` refuses an
  untagged commit unless `--allow-untagged` (store-release's own CI).
- CI (`test (stdlib)`, `test (strict)`, `test (macos)`, `lint`, `workflows`, and the
  advisory `canary (fud-ai)` against fud-ai's real store files), CodeQL
  (`Analyze (python)`, `Analyze (actions)`), dependency review, Dependabot (grouped,
  with a seven-day cooldown), the dispatch-only Release workflow
  (`scripts/release_check.py`: only GitHub Actions check runs from `ci.yml` and
  `codeql.yml` count; one `gh release create` makes the tag and the release) and the
  re-run probe. Every job, and every rendered caller job, declares its permissions.
- Docs: config, switches, secrets, workflows, re-runs, store plan, verification,
  release process, yanked versions, provenance and roadmap.

### App actions required

Fud AI's migration (one PR in fud-ai, validated by dispatches before merge):

- add `store/store-release.toml` (`examples/fud-ai/store-release.toml`);
- replace `ios-release.yml` and `android-release.yml` with the rendered callers at
  the release SHA, add `store-checks.yml`, delete `store-automation.yml`;
- move `scripts/store/validate_catalog.py` unchanged to `scripts/ci/check_catalog.py`;
  delete the rest of `scripts/store/`, `scripts/release_notes.py`,
  `requirements-store.in/.txt`, `store/gates.env.example` and the unused
  `store/metadata` placeholders;
- in `.github/dependabot.yml`, delete the root pip entry and add a `store-release`
  group before the catch-all github-actions group (a `cooldown` of 7 days on it is
  suggested: these pins run with the app's signing credentials);
- trim `store/README.md` to Fud AI facts, linking to these docs.

No switch, secret or variable changes.

### Verification

- CI on `main` at `6eaa676351feeb37ed42f0d648983feaaa051778`, the code this release
  tags (the release commit changes only this file and `docs/rerun.md`):
  [CI 39473468499](https://github.com/apoorvdarshan/store-release/actions/runs/39473468499)
  (`test (stdlib)`, `test (strict)`, `test (macos)`, `lint`, `workflows`, and the
  advisory `canary (fud-ai)`, which ran the store checks on fud-ai's `main` and found
  its pinned files intact) and
  [CodeQL 39473468458](https://github.com/apoorvdarshan/store-release/actions/runs/39473468458)
  (`Analyze (python)`, `Analyze (actions)`). `release.yml` checks the required ones
  again on the release commit.
- Re-run probe: passed with `--failed`
  ([39459054072](https://github.com/apoorvdarshan/store-release/actions/runs/39459054072),
  and again on `6eaa676`:
  [39473507701](https://github.com/apoorvdarshan/store-release/actions/runs/39473507701))
  and `--job`
  ([39459383671](https://github.com/apoorvdarshan/store-release/actions/runs/39459383671));
  see [docs/rerun.md](docs/rerun.md#results).
- Fud AI's validation dispatches and their store plans are the gate of Fud AI's
  migration PR, the next phase; they have not run yet. A defect they find ships as
  v0.1.1 (tags never move).

[Unreleased]: https://github.com/apoorvdarshan/store-release/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/apoorvdarshan/store-release/releases/tag/v0.2.0
[0.1.0]: https://github.com/apoorvdarshan/store-release/releases/tag/v0.1.0
