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
- `store_release.__version__` must equal the tag.

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
(`requirements.txt`) `release-path`, a `requirements-checks.txt` bump `checks-only`.
Dependabot waits seven days after a release before proposing it (security updates
are not delayed), and its grouped PR moves every file that pins an action at once,
which is what `test (strict)` requires: one pin per action everywhere. A new
third-party action in the release path also needs adding to `RELEASE_ACTIONS` in
`tests/workflows/test_workflows.py`.
The locks are regenerated with `pip-compile --generate-hashes` (pip-tools 7.6.2) on
Python 3.13. The Apple WWDR G3 and bundletool checksums are updated by hand, in a PR
with Impact `validation`.

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
      `lint`, `workflows`, `Analyze (python)` and `Analyze (actions)` on the commit
      concluded `success` (still running counts as a failure: wait, then dispatch
      again). Only check runs that GitHub Actions created in the commit's `ci.yml`
      and `codeql.yml` runs count. The canary is advisory and not checked.
3. Then the run builds the release assets from that commit (below) and one call
   (`gh release create vX.Y.Z --target <commit>`) creates the tag on that commit and
   publishes the GitHub Release with them, marked Latest. gh uploads the assets to a draft
   and publishes it last (an immutable release takes no assets once published), so the
   tag exists only once the release is complete. The step then confirms the tag points at
   the commit.
4. Verify the result ([below](#verifying-a-release)).

## Release assets

Every release carries four files, made in the Release run after every precondition held:

| File | Made by |
|---|---|
| `store-release-X.Y.Z-source.tar.gz` | `git archive --format=tar.gz --prefix=store-release-X.Y.Z/ <commit>` |
| `store-release-X.Y.Z-source.tar.gz.sha256` | `sha256sum` of the archive |
| `store-release-X.Y.Z-source.tar.gz.sigstore.json` | `cosign sign-blob --bundle` (Sigstore keyless; the identity is `release.yml` on `refs/heads/main`) |
| `store-release-X.Y.Z-source.tar.gz.intoto.jsonl` | `actions/attest-build-provenance` (SLSA build provenance, also stored by GitHub) |

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
sha256sum -c "store-release-$v-source.tar.gz.sha256"
gh attestation verify "store-release-$v-source.tar.gz" --repo "$repo" \
  --signer-workflow "$repo/.github/workflows/release.yml"
cosign verify-blob "store-release-$v-source.tar.gz" --bundle "store-release-$v-source.tar.gz.sigstore.json" \
  --certificate-identity "https://github.com/$repo/.github/workflows/release.yml@refs/heads/main" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The release must be published (not a draft), immutable and Latest, with the four assets;
the tag must be the commit the Release run was dispatched on, whose `test (stdlib)`,
`test (strict)`, `test (macos)`, `lint`, `workflows`, `Analyze (python)` and
`Analyze (actions)` passed. Users verify the same way ([verification.md](verification.md)).

## When the Release run fails

- Before the last step (a precondition failed): nothing was created. Fix the cause
  (or wait for CI), then dispatch again.
- In the last step: check what exists with
  `gh release view vX.Y.Z --repo apoorvdarshan/store-release` and
  `gh api repos/apoorvdarshan/store-release/git/ref/tags/vX.Y.Z`.
  - Neither exists: dispatch again.
  - Both exist and the tag points at the commit the run was for: the release went
    through (the call failed after GitHub created it). Nothing to do.
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
