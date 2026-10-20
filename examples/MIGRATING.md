# Moving an app to store-release (the five ported apps)

Each `examples/<app>/` holds the app's `store-release.toml` and its rendered callers. The
app's migration pull request (not part of store-release) does the same for every app:

1. Add `store/store-release.toml` (the example, unchanged) and any `app-files/` the example
   has, at the same paths.
2. Replace `.github/workflows/ios-release.yml` (and Verceltics' `android-release.yml`) with
   the callers rendered at the release's commit:
   `python3 -I -u <store-release>/run.py --app-root . callers render --platform ios --sha <sha> --version <x.y.z> --out .github/workflows/ios-release.yml`.
   Add `store-checks.yml` the same way and remove the app's former store check workflow
   (`store-automation.yml` or `store-checks.yml`).
3. Remove the release tooling store-release now runs: `scripts/store/`, `scripts/release_notes.py`,
   `requirements-store.in/.txt`, `store/gates.env.example`, and the Dependabot pip entry
   for them. Keep the app's own test runners and CI scripts (`scripts/test-ios.sh`,
   `scripts/ci/*`) and its gate workflows (`ios-tests.yml`, Verceltics' `ci.yml`).
4. Dispatch **iOS Release** (and Verceltics' **Android Release**) on the branch: the build
   must sign, export and validate, and `store-plan` must end CLEAN (or with problems the
   app's owner reviewed). Compare with a dispatch of the old pipeline.

Switch names, values and secret names never change. Rolling back is reverting that pull
request.

## Per app

| App | Add | Notes for the migration |
|---|---|---|
| Scowld | none | The source archive is now signed by `ios-source-archive.yml` in store-release: verify with `--certificate-identity-regexp '^https://github\.com/apoorvdarshan/store-release/\.github/workflows/ios-source-archive\.yml@[0-9a-f]{40}$' --certificate-github-workflow-repository apoorvdarshan/scowld` ([verification](../docs/verification.md)); v2.1.0 and earlier keep the old identity. Update the commands in README, SECURITY.md and store/README.md. |
| Verceltics | `scripts/ci/release_gate_tests.sh` | `scripts/test.sh` must stop discovering `scripts/store` (gone) so the required `catalog` check still finds tests. The Android artifacts are signed by store-release's `android-publish.yml` (as Fud AI's). |
| ABYSM | none | Its Atheris targets in `fuzz/` import `scripts/store/*`: keep those scripts' fuzzing out of the migration until store-release carries fuzz targets (not in v0.2.0). `scripts/ci` tests for `test-ios.sh` stay. |
| PCM Notes | `scripts/ci/check_offline_notes.sh` | Its `fuzz/` targets for the store scripts (`fuzz_build_selection`, `fuzz_release_gates`, `fuzz_release_notes`) go with the scripts; `fuzz_test_summary`, `fuzz_input` stay. `main` needs an admin merge (its ruleset). |
| Quit All | none | With `scripts/store` gone, check whether any Python remains for the required `Analyze (python)` check before merging (a ruleset change needs the owner's approval). |
