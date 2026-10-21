# Releasing store-release

How store-release itself is versioned, released and verified, and how apps receive a new
release. The commands name this repository, `apoorvdarshan/store-release`; a fork uses its
own.

## Versions

- SemVer tags `vX.Y.Z`, created by the **Release** workflow (a convention: nothing
  blocks creating a tag) and never moved. Every fix is a new patch.
- While on 0.x: MINOR for a new app's features or config keys (with defaults that
  keep current behaviour) and for any `config-breaking` change; PATCH for fixes and
  pin bumps.
- `store_release.__version__` must equal the tag. It is also the version of the PyPI
  package, `store-release` ([PyPI](#pypi)).

## CHANGELOG

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/): an `## [Unreleased]`
section, then one `## [X.Y.Z] - YYYY-MM-DD` section per tag. Each pull request adds
its entry under `[Unreleased]`. Each release section has:

- `Impact:` one of
  - `checks-only`: PR validation or docs only
  - `validation`: build, sign or verify paths that validation dispatches exercise
  - `release-path`: App Store Connect, Play or GitHub Release writes, or other
    tag-only paths
  - `config-breaking`: an app must edit its config
- `### Added` / `### Changed` / `### Fixed`
- `### App actions required`: exact config or caller edits
- `### Verification`: run URLs for CI, the canary, the re-run probe (when the
  workflows changed), and the app validation dispatches with their store-plan results

Dependabot bumps ship as patches: an action bump has Impact `release-path` or
`validation` depending on which jobs use the action, a runtime lock bump
(`requirements.txt`) `release-path`, a `requirements-checks.txt` or `requirements-dist.txt`
bump `checks-only` (the dist lock builds store-release's own sdist and wheel; apps never
install it). Each extra in `pyproject.toml` allows one major version and must hold the
locked version (`tests/unit/test_packaging.py`): a runtime lock bump within the major
leaves `pyproject.toml` as it is (Dependabot widens a range only when the new version falls
outside it), and a new major fails that test until its extra's floor moves to the new lock
and its cap past it.
Dependabot waits seven days after a release before proposing it (security updates
are not delayed), and its grouped PR moves every file that pins an action at once,
which is what `test (strict)` requires: one pin per action everywhere. A new
third-party action in the release path also needs adding to `RELEASE_ACTIONS` in
`tests/workflows/test_workflows.py`.
The locks are regenerated with `pip-compile --generate-hashes` (pip-tools 7.6.2) on
Python 3.13. `requirements-dist.in` lists SecretStorage and jeepney, which twine's keyring
needs on Linux only, so that the lock is complete when it is compiled on macOS. The Apple
WWDR G3 and bundletool checksums are updated by hand, in a PR with Impact `validation`.

## Cutting a release

1. A PR sets `__version__`, turns `[Unreleased]` into `## [X.Y.Z] - <today>` with its
   Impact line, and adds the verification URLs. Merge it once CI is green.
2. Dispatch **Release** on `main` with `version=X.Y.Z`
   (`gh workflow run release.yml -f version=X.Y.Z`). `scripts/release_check.py` fails
   unless:
   1. the run is on `refs/heads/main`;
   2. the version is X.Y.Z and equals `__version__`;
   3. no tag `vX.Y.Z` exists and the version is greater than every `v*` tag;
   4. `CHANGELOG.md` has exactly one dated section for it, non-empty, with an
      `Impact:` line (its body becomes the GitHub Release notes);
   5. [yanked.md](yanked.md) does not list it;
   6. the newest check run of `test (stdlib)`, `test (strict)`, `test (macos)`,
      `lint`, `package`, `workflows`, `Analyze (python)` and `Analyze (actions)` on the
      commit concluded `success` (still running counts as a failure: wait, then dispatch
      again). Only check runs that GitHub Actions created in the commit's `ci.yml`
      and `codeql.yml` runs count. The canary is advisory and not checked.
3. Meanwhile the `dists` job builds the sdist and the wheel from the commit, without
   write access or an OIDC token (`scripts/dists.py build` and `check`, with the tools
   `requirements-dist.txt` locks). Once every precondition holds, the `release` job
   archives the source, signs and attests the archive and the dists (below), and one call
   (`gh release create vX.Y.Z --target <commit>`) creates the tag on that commit and
   publishes the GitHub Release with all of them, marked Latest. gh uploads the assets to a
   draft and publishes it last (an immutable release takes no assets once published), so
   the tag exists only once the release is complete. The step then confirms
   (`scripts/dists.py release-check --complete`) that the release is published and
   immutable, its tag is the commit, and its assets are exactly the run's files.
4. The `pypi` job publishes the release's sdist and wheel to PyPI ([PyPI](#pypi)).
5. Verify the result ([below](#verifying-a-release)).

## Release assets

Every release carries eleven files, made in the Release run from the commit (the archive and
the signatures after every precondition held):

| File | Made by |
|---|---|
| `store-release-X.Y.Z-source.tar.gz` | `git archive --format=tar.gz --prefix=store-release-X.Y.Z/ <commit>` |
| `store-release-X.Y.Z-source.tar.gz.sha256` | `sha256sum` of the archive |
| `store-release-X.Y.Z-source.tar.gz.sigstore.json` | `cosign sign-blob --bundle` (Sigstore keyless; the identity is `release.yml` on `refs/heads/main`) |
| `store-release-X.Y.Z-source.tar.gz.intoto.jsonl` | `actions/attest-build-provenance` (SLSA build provenance, also stored by GitHub) |
| `store_release-X.Y.Z.tar.gz` | the sdist: `scripts/dists.py build` (`python -m build`, hatchling) in the `dists` job |
| `store_release-X.Y.Z.tar.gz.sha256` | `sha256sum` of the sdist |
| `store_release-X.Y.Z.tar.gz.sigstore.json` | `cosign sign-blob --bundle`, as for the archive |
| `store_release-X.Y.Z-py3-none-any.whl` | the wheel, built from the sdist in the same step |
| `store_release-X.Y.Z-py3-none-any.whl.sha256` | `sha256sum` of the wheel |
| `store_release-X.Y.Z-py3-none-any.whl.sigstore.json` | `cosign sign-blob --bundle`, as for the archive |
| `store_release-X.Y.Z.intoto.jsonl` | `actions/attest-build-provenance` for the sdist and the wheel |

The sdist and the wheel are built with `SOURCE_DATE_EPOCH` set to the commit's time, so
rebuilding the tag with the locked tools on the same platform gives the same files.

## Verifying a release

After every release, check the tag, the assets and CI on the tagged commit:

```bash
v=X.Y.Z
repo=apoorvdarshan/store-release
sha=$(gh api "repos/$repo/git/ref/tags/v$v" --jq .object.sha)       # the tag (lightweight: the commit)
gh release view "v$v" --repo "$repo" --json isDraft,isImmutable,assets \
  --jq '{isDraft, isImmutable, assets: [.assets[].name]}'
gh api "repos/$repo/releases/latest" --jq .tag_name                 # Latest: v$v
gh run list --repo "$repo" --commit "$sha" --json name,conclusion   # CI and CodeQL on that commit
gh release download "v$v" --repo "$repo" --dir "store-release-$v"
cd "store-release-$v"
for file in "store-release-$v-source.tar.gz" "store_release-$v.tar.gz" "store_release-$v-py3-none-any.whl"; do
  sha256sum -c "$file.sha256"
  gh attestation verify "$file" --repo "$repo" --signer-workflow "$repo/.github/workflows/release.yml"
  cosign verify-blob "$file" --bundle "$file.sigstore.json" \
    --certificate-identity "https://github.com/$repo/.github/workflows/release.yml@refs/heads/main" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
done
curl -s "https://pypi.org/pypi/store-release/$v/json" \
  | python3 -c 'import json, sys; [print(f["digests"]["sha256"] + "  " + f["filename"]) for f in json.load(sys.stdin)["urls"]]' \
  | sha256sum -c -                                                   # PyPI's files are these
```

The release must be published (not a draft), immutable and Latest, with the eleven assets;
the tag must be the commit the Release run was dispatched on, whose `test (stdlib)`,
`test (strict)`, `test (macos)`, `lint`, `package`, `workflows`, `Analyze (python)` and
`Analyze (actions)` passed; PyPI must list exactly the release's sdist and wheel for the
version. Users verify the same way ([verification.md](verification.md)).

## PyPI

The `pypi` job runs after the GitHub Release exists, in the `pypi` environment, with
`id-token: write` and `contents: read` and no secret. It

1. downloads the release's sdist and wheel, checks that `vX.Y.Z` is published and
   immutable, that its tag is this run's commit and that the files are its assets
   (`scripts/dists.py release-check`), then checks each against its `.sha256` asset and
   its build provenance (`gh attestation verify`: this repository's `release.yml` on
   `refs/heads/main`, at this commit, on a GitHub-hosted runner);
2. asks PyPI what it has for the version (`scripts/dists.py pypi-plan`): a file PyPI has
   with the same SHA-256 is already published and skipped, a file with another digest (or
   one this release did not build) fails the job, and the rest are uploaded;
3. uploads them with `pypa/gh-action-pypi-publish` through Trusted Publishing (PyPI swaps
   the job's OIDC token for a short-lived upload token: there is no API token anywhere) and
   PEP 740 attestations, signed with the same identity;
4. waits until PyPI serves both files with the release's digests, each attested by this
   repository's `release.yml` (`scripts/dists.py pypi-verify`).

### The trusted publisher

PyPI accepts the upload only from the publisher registered for the project. Before the
first PyPI release, add it as a pending publisher (it becomes the project's publisher on
the first upload) at <https://pypi.org/manage/account/publishing/>, and keep it as is:

| PyPI field | Value |
|---|---|
| PyPI Project Name | `store-release` |
| Owner | `apoorvdarshan` |
| Repository name | `store-release` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

A pending publisher does not reserve the name: until the first upload creates the project,
anyone can register `store-release` on PyPI (the pending publisher then stops working), so
run that first release soon after adding it.

`pypi-verify` warns when PyPI's attestations name no environment: the publisher was
registered without `pypi`, so any job of `release.yml` could publish; add the environment
on PyPI. Restrict the `pypi` environment to `main` (Settings, Environments, `pypi`,
Deployment branches and tags: Selected branches, `main`), so no other branch's run of
`release.yml` can deploy to it. Do it before the first PyPI release: otherwise GitHub
creates the environment, unrestricted, on the job's first run. The `PUT` creates it when it
does not exist yet:

```bash
gh api -X PUT repos/apoorvdarshan/store-release/environments/pypi \
  -F 'deployment_branch_policy[protected_branches]=false' -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/apoorvdarshan/store-release/environments/pypi/deployment-branch-policies \
  -f name=main -f type=branch
```

### When the pypi job fails

The GitHub Release is complete and stays valid: apps pin the commit, so nothing they run
changes. Fix the cause (the trusted publisher missing or different from the table above,
PyPI unavailable), then **Re-run failed jobs** on that Release run. The re-run downloads
the same release files, skips what PyPI already has with the same SHA-256 and uploads the
rest, so it never publishes a file twice or a different file. Never dispatch Release again
for the version (the tag exists, so the precondition check refuses it).

- `PyPI has <file> with another SHA-256 than this release's file`: PyPI never lets a
  file name be replaced or reused, so that version's PyPI files cannot match the release.
  Release the next patch.
- GitHub keeps a run re-runnable for 30 days. After that, release the next patch: PyPI's
  files come only from `release.yml`, with their attestations, never from a hand upload.
- `pypi-verify` timing out after a successful upload (PyPI slow to list the files or the
  attestations): re-run the job; it skips the upload and checks again.

## When the Release run fails

- Before the last step of the `release` job (the `dists` job or a precondition failed):
  nothing was created. Fix the cause (or wait for CI), then dispatch again.
- In the last step: when `gh release create` fails, `scripts/dists.py release-check` still
  accepts the release if GitHub published it anyway (published, immutable, tagged on the
  run's commit, with exactly the run's eleven files), and the `pypi` job runs. Otherwise
  check what exists with `gh release view vX.Y.Z --repo apoorvdarshan/store-release` and
  `gh api repos/apoorvdarshan/store-release/git/ref/tags/vX.Y.Z`.
  - Neither exists: dispatch again.
  - Both exist and the tag points at the commit the run was for, but release-check
    failed (GitHub did not answer for a minute, or the release differs from the run's
    files): the GitHub Release stands as published, and only PyPI lacks the version. The
    `pypi` job cannot run for it again (the preconditions refuse the existing tag), so
    release the next patch to publish to PyPI.
  - The tag points at another commit (a tag pushed between the check and the
    release): yank that version ([yanked.md](yanked.md)) and release the next patch.
  - A tag exists without a release: tags never move, so release the next patch, and
    yank the version if anything pinned it.
  - **A stuck draft:** the run stopped (cancelled, timed out or the runner was lost) after
    gh created the draft and before it published it. A draft makes no tag, so the version
    is still free. Check, then delete the draft and dispatch again:

    ```bash
    gh release list --repo apoorvdarshan/store-release --json tagName,isDraft \
      --jq '.[] | select(.isDraft) | .tagName'                       # lists vX.Y.Z
    gh api repos/apoorvdarshan/store-release/git/ref/tags/vX.Y.Z     # 404: no tag yet
    gh release delete vX.Y.Z --repo apoorvdarshan/store-release --yes
    gh workflow run release.yml --repo apoorvdarshan/store-release -f version=X.Y.Z
    ```

    Never publish the stuck draft by hand: its assets may be incomplete, and publishing
    would create the tag outside the checked run. If the tag lookup does not return 404,
    the release went through after all; check it as in the bullets above.
- In the `pypi` job: the GitHub Release exists and is valid; see
  [When the pypi job fails](#when-the-pypi-job-fails).

## After the first tag

Protect the tags (creating tags stays allowed, for `release.yml`):

```bash
gh api -X POST repos/apoorvdarshan/store-release/rulesets --input - <<'JSON'
{
  "name": "Release tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
  "rules": [{"type": "deletion"}, {"type": "update"}, {"type": "non_fast_forward"}],
  "bypass_actors": []
}
JSON
```

Immutable releases are enabled before the first release, so every release is
published immutable; confirm with `gh api repos/apoorvdarshan/store-release/immutable-releases`.

## How apps receive a release

1. Dependabot (weekly, grouped as `store-release`) opens one PR per app that moves
   every store-release reference in its three callers to the new SHA and comment.
2. That PR's Store checks run the new version against the app's real data, including
   the pins and callers.
3. By Impact:
   - `checks-only`: merge.
   - `validation` or `release-path`: dispatch the app's iOS Release (with the next
     unapproved `marketing_version`) and Android Release on the Dependabot branch.
     Both must be green and both store-plans clean. A manual dispatch gets the normal
     Actions secrets; a Dependabot-started run never does.
   - `config-breaking`: push the listed config edits onto the branch first, then
     validate.
4. Merge with `--match-head-commit`.
5. Rollback is reverting the bump PR. The old SHA stays valid, because tags never move
   and pins are SHAs.
6. Push an app's `ios-v*` and `android-v*` tags one at a time: GitHub creates no push
   events when more than three tags are pushed at once.

## Yanking

1. Add `- vX.Y.Z — YYYY-MM-DD — <reason> (use vX.Y.Z+1)` to [yanked.md](yanked.md)
   through a normal PR.
2. Ship the fixed patch.

From then on, apps still pinned to the yanked version fail their Store checks, their
release runs print a warning (a release never depends on mutable remote state), and
Dependabot's bump PRs to the fixed version pass.
