# Reusable workflows

Ten single-job reusable workflows (`on: workflow_call`). An app calls them from three
thin caller workflows that `run.py callers render` writes from its config
([Fud AI's](../examples/fud-ai/callers/)); each caller job calls exactly one of them,
so **Re-run failed jobs** works per stage ([rerun.md](rerun.md)).

```
iOS Release (.github/workflows/ios-release.yml in the app)
  push <ios tag>:  <gate jobs> ─┐  (the app's own test workflows, unchanged)
                   prepare ─────┴─► build ─► wait-for-build ─► app-store-connect ─► github-release
  Run workflow:    prepare ─► build (validate: archive, cloud-signed export, altool --validate-app)
                           └► store-plan (read-only, report-only)

Android Release (.github/workflows/android-release.yml)
  push <android tag>:  gates ─► <gate jobs> (only when it builds) ─► release
  Run workflow:        gates ─► release (validate: signed AAB + APK, verified)
                            └► store-plan (read-only, report-only)
  master switch off on a tag: gates resolves build=false; the rest is skipped (a green no-op)

Store checks (.github/workflows/store-checks.yml; PRs, main pushes, Run workflow)
  checks ─► store-release (a plain aggregator job with a stable name)
```

Merging never starts a release. Only a tag push releases; a dispatch only validates
and plans.

## The workflows

| File | Inner job | Runner | Timeout | Inputs (required ones in bold) | Outputs |
|---|---|---|---|---|---|
| `ios-prepare.yml` | `prepare` | ubuntu-latest | 15 | `config`, `marketing_version` (dispatch override) | `mode`, `create_version`, `build`, `upload_build`, `listing`, `whats_new`, `screenshots`, `creative`, `submit`, `public_release`, `asc`, `poll_seconds`, `wait_minutes`, `marketing_version`, `build_number`, `tool_sha` |
| `ios-build.yml` | `build` | `inputs.runner` (default `xcode-27`) | 90 | `config`, `runner`, **`gates`**, **`mode`**, **`marketing_version`**, **`build_number`**, **`tool_sha`** | `upload_started_at` |
| `ios-wait-for-build.yml` | `wait-for-build` | ubuntu-latest | 310 | `config`, **`marketing_version`**, **`build_number`**, **`upload_started_at`**, **`wait_minutes`**, **`poll_seconds`**, **`tool_sha`** | `build_id` |
| `ios-app-store-connect.yml` | `app-store-connect` | ubuntu-latest | 60 | `config`, **`gates`**, **`marketing_version`**, **`build_number`**, **`build_id`**, **`upload_build`**, **`listing`**, **`whats_new`**, **`screenshots`**, **`creative`**, **`submit`**, **`public_release`**, **`tool_sha`** | |
| `ios-github-release.yml` | `github-release` | ubuntu-latest | 10 | `config`, **`gates`**, **`marketing_version`**, **`uploaded_build`**, **`tool_sha`** | |
| `ios-store-plan.yml` | `store-plan` | ubuntu-latest | 15 | `config`, **`marketing_version`**, **`tool_sha`** | |
| `android-gates.yml` | `gates` | ubuntu-latest | 10 | `config` | `mode`, `build`, `github_release`, `listing`, `whats_new`, `screenshots`, `production`, `play_upload`, `play_tracks`, `tool_sha` |
| `android-publish.yml` | `release` | ubuntu-latest | 60 | `config`, **`gates`**, **`mode`**, **`build`**, **`github_release`**, **`listing`**, **`whats_new`**, **`screenshots`**, **`production`**, **`play_upload`**, **`play_tracks`**, **`tool_sha`** | |
| `android-store-plan.yml` | `store-plan` | ubuntu-latest | 15 | `config`, **`tool_sha`** | |
| `checks.yml` | `checks` | ubuntu-latest | 15 | `config` | |

`config` defaults to `store/store-release.toml`. Secrets per workflow:
[secrets.md](secrets.md). `checks.yml` runs the iOS and Android steps only for the
platforms the config has (`config export --job checks` sets `STORE_RELEASE_HAS_IOS`
and `STORE_RELEASE_HAS_ANDROID`), so an iOS-only or Android-only app passes it.

## How every job starts

1. Check that the secrets every run of the job needs are set (as booleans; the
   values are never read for this). Secrets that only some switches need (in
   `prepare`, `gates` and the Play steps of `release`) are checked once the
   switches show they are needed.
2. Check out the app's config only (`filter: blob:none`, non-cone sparse) into `app/`.
3. Check out `${{ job.workflow_repository }}` at `${{ job.workflow_sha }}`, whole,
   into `.store-release/`. The YAML that runs and the Python it runs always come from
   one commit, the SHA the app pinned.
4. `job-setup` (`.github/actions/job-setup`): Python 3.13, `tool check` (the checkout
   is this workflow's commit, and in every job after prepare/gates, the same commit as
   theirs), `config check`, then this job's sparse checkout of the app
   (`paths --job <id>`; [config.md](config.md)) and `config export`, which writes the
   validated `STORE_RELEASE_*` values to `$GITHUB_ENV`.
5. `stage check` in every job after prepare/gates: re-validate each input (below).

The app checkout is blobless and sparse, so files outside a job's patterns (such as
a large asset corpus) are never downloaded. Every app repository is public, so the
lazy blob fetch needs no credentials.

## Values between jobs

Job outputs are scalars with a fixed format; no JSON and no app strings (names,
paths, titles). Each job re-derives what it can from the config at `github.sha`, and
`stage check` re-validates every input and fails closed when one is empty, which is
how an output dropped by secret masking shows up.

| Scalar | Format |
|---|---|
| `mode` | `release` on a tag push, `validate` on a dispatch; anything else fails |
| booleans | `true` or `false` exactly |
| `marketing_version` | `^[0-9]+(\.[0-9]+){0,2}$` |
| `build_number`, `uploaded_build` | digits (`uploaded_build` may be empty) |
| `poll_seconds` / `wait_minutes` | digits within 10–600 / 10–300 |
| `upload_started_at` | epoch seconds |
| `build_id` | a UUID, or empty |
| `play_tracks` | empty, or `internal`, `alpha`, `production` (in that order, unique) |
| `tool_sha` | 40 hex, equal to the job's `job.workflow_sha` |
| `gates` | `<job>=<result>` pairs, one per configured gate job |

**The gate rule** (`stage check`): the names in `gates` must be exactly the configured
`gate_jobs`. On a release every result must be `success`; on a validation dispatch
all must be `skipped`, or all `success`; a mix fails. The caller's `if:` says the
same thing; the check is defence in depth against a hand-edited caller.

## Rules for every workflow

- `permissions: contents: read` at the top, exactly one job with explicit
  `permissions`, `runs-on` and `timeout-minutes`, no `concurrency` (the callers own
  it), `defaults: run: shell: bash`.
- Every third-party action is pinned to a full commit SHA with a `# vX.Y.Z` comment,
  and to the same pin in every file.
- `${{ }}` never appears inside `run:`; inputs, outputs, variables and secrets reach
  shell only through `env:`. Secrets live in the `env:` of the steps that use them.
- Python always runs as `python3 -I -u .store-release/run.py`; lock installs use
  `--require-hashes`, and never on macOS (the build job's tooling is stdlib-only).

The workflow tests (`tests/workflows/`) enforce all of this, and `actionlint` runs in CI.

## Grants and check names

A caller job grants its reusable workflow's job its permissions; the inner job never
asks for more.

| Caller job | Grant |
|---|---|
| `prepare`, `build`, `wait-for-build`, `app-store-connect`, `store-plan` | `contents: read` |
| `github-release` | `contents: write` |
| `gates` | `contents: read`, `actions: read` |
| `release` | `contents: write`, `id-token: write`, `attestations: write`, `actions: read` |
| `checks` | `contents: read` |

Check runs are named `<caller job> / <inner job>`: `prepare / prepare`,
`build / build`, `wait-for-build / wait-for-build`, `app-store-connect /
app-store-connect`, `github-release / github-release`, `store-plan / store-plan`,
`gates / gates`, `release / release`, `checks / checks`, plus the plain
`store-release` aggregator in Store checks (`permissions: {}`: it only reads the
checks job's result). The app's own gate jobs keep their names.
No caller job may be named `web` or `android` (a plain job with a required check's
name would satisfy it by accident); the caller check refuses them.

## Caller checks

- `callers render --platform ios|android|checks --sha SHA --version X.Y.Z
  --app-repository OWNER/REPO` writes a caller from the config; it is the only source
  of caller text. It refuses a `SHA` that the `vX.Y.Z` tag does not point at;
  `--allow-untagged` is for store-release's own CI, which renders its current commit.
- `caller check --pr` (Store checks) compares each caller with the rendered one as
  YAML (comments do not count) and reports the differing keys; every store-release
  reference must end in `@<sha> # v<version>`, and store-release's `v<version>` tag
  must point at `<sha>`.
- `pins check` (Store checks): every store-release reference in every app workflow
  is this commit with this version's comment, and the `v<version>` tag points at
  this commit.
- `caller check --runtime` (prepare, gates): the running caller pins every
  store-release reference to this job's `job.workflow_sha`, and its concurrency is
  exactly `group: <platform>-release-${{ github.event_name == 'push' && 'release' ||
  github.run_id }}`, `cancel-in-progress: false`, `queue: max`. A `v<version>` tag
  that points elsewhere, is missing or cannot be read is only a warning here.
- The tag checks read `GET /repos/apoorvdarshan/store-release/git/ref/tags/v<version>`
  (annotated tags are followed to their commit) with the job's read-only
  `GITHUB_TOKEN`. In Store checks a failed read fails the check; re-run it. They run
  inside the pinned code, so they catch mistakes, not a hostile pin
  ([verification.md](verification.md#store-release-itself)).
- `yanked check`: a pinned version listed in [yanked.md](yanked.md) fails Store
  checks and is a warning in prepare/gates (a release never depends on mutable
  remote state).

## Startup failures

These make GitHub reject the run before any job starts, so it has no check runs at
all, and a required check waits at "Expected" (it fails closed):

- a `uses:` pin that does not exist
- passing a secret the reusable workflow does not declare
- not passing a `required: true` secret
- an inner job asking for more permissions than its caller job grants

The rendered callers, the PR caller check and the examples test prevent all four.
When it happens anyway, the run page shows the reason at the top ("Invalid
workflow file").
