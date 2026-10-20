# store-release

Reusable GitHub Actions workflows and Python tooling that build, sign and publish iOS
and Android apps to App Store Connect and Google Play, pinned by commit SHA.

It started as [Fud AI](https://github.com/apoorvdarshan/fud-ai)'s release pipeline
and keeps its behaviour exactly ([provenance](docs/provenance.md)). From v0.2.0 it
also runs Scowld, Verceltics, ABYSM, PCM Notes and Quit All, each through a port of
its own pipeline ([flows](docs/flows.md), [examples](examples/)).

## How an app uses it

An app keeps three things:

- `store/store-release.toml`: where its store files are and its fixed identities
  (bundle id, package, team, upload certificate) ([config](docs/config.md)).
- Its switches as repository variables and its credentials as repository secrets
  ([switches](docs/switches.md), [secrets](docs/secrets.md)).
- Three thin caller workflows, rendered from the config, that call this repository's
  reusable workflows at a pinned commit:

```yaml
  prepare:
    uses: apoorvdarshan/store-release/.github/workflows/ios-prepare.yml@<40-hex sha> # v0.2.0
    permissions:
      contents: read
    secrets:
      APP_STORE_CONNECT_API_KEY_ID: ${{ secrets.APP_STORE_CONNECT_API_KEY_ID }}
      # ...every secret mapped by name; never secrets: inherit
```

```bash
python3 -I -u store-release/run.py --app-root . callers render --platform ios \
  --sha <sha> --version 0.2.0 --out .github/workflows/ios-release.yml
```

[Fud AI's callers](examples/fud-ai/callers/) are the reference. A tag push
(`ios-vX.Y`, `android-vX.Y`) releases; **Run workflow** only validates the build and
prints a read-only plan of what a tag run would change ([plan](docs/plan.md)).
Store checks validate the store data, the config and the callers on every pull
request. How the workflows fit together: [workflows](docs/workflows.md).

## Safety model

- **Nothing reaches a store unless its switch is on.** Missing means off; an
  invalid value fails the run, even with the master switch off.
- **Checks before builds.** Everything that can fail runs before anything is built
  or uploaded; a manual run never uploads.
- **One pinned commit.** Every job checks out store-release at `job.workflow_sha`,
  so the workflow YAML and the Python it runs are the commit the app pinned and
  reviewed.
- **Least privilege.** Explicit secret mapping, explicit permissions per job,
  credentials only under `RUNNER_TEMP` and removed afterwards, app hooks run with
  credentials scrubbed from their environment, failure logs redacted before upload.
- **Read-only by construction** where it should be: the build wait, the preflights
  and the store plan use clients that refuse any write.

## Versioning

SemVer tags `vX.Y.Z`, created by the Release workflow (by convention) only after CI
passed on that commit, never moved; apps pin the commit SHA with a `# vX.Y.Z` comment (Store checks
fail unless the tag points at that commit) and receive updates through Dependabot. Changes and their impact on apps:
[CHANGELOG](CHANGELOG.md). Releasing, yanking and how apps update:
[release process](docs/release-process.md), [yanked versions](docs/yanked.md).
Re-runs: [rerun](docs/rerun.md). Verifying Android release files:
[verification](docs/verification.md).

## Development

```bash
python3 -I -u run.py selftest --platform all            # stdlib only
python3 -m pip install --require-hashes -r requirements.txt -r requirements-checks.txt
python3 -I -u run.py selftest --platform all --suite unit --suite apps --suite parity \
  --suite workflows --require-pillow --strict
```

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT License](LICENSE)
