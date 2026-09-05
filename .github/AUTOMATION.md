# Release automation pilot

Development is pushed directly to `main`. A successful `Validate` run examines
commits since the most recent stable GitHub release: `feat:` prepares a minor
release and `fix:` a patch release. It creates or updates one `automation/release`
PR; no push itself creates a tag or a GitHub release.

Merge that generated PR when a release is wanted. Its own successful validation
then creates the no-`v` tag and GitHub release. `chore:`, `refactor:`, `docs:`,
`test:` and `ci:` changes never create a release PR.

Before enabling a live release, add a fine-grained `RELEASE_BOT_TOKEN` Actions
secret. It needs repository `Contents: read/write` and `Pull requests: read/write`;
the bot identity must be allowed to push `automation/*`. A GitHub App token is
preferred. The token is deliberately not used for publishing tags or releases.

Run **Prepare release** manually with `dry_run` enabled to inspect the calculated
version and notes without opening a PR.
