# Conventions

## Public mirror and infrastructure privacy

- Treat tracked files and Git metadata as public: this repository is intended for a GitHub mirror. Never add private GitLab or registry addresses, internal domains or IPs, personal checkout paths, runner/cluster names, credentials, tokens, or certificates/private keys from the development environment.
- Keep deployment-specific values in environment variables, an ignored `.env` file, or ignored Compose overrides. Use GitLab's predefined CI variables for registry URLs and credentials. Defaults must work without access to private infrastructure; keep registry pulls optional through `DNS_IMAGE`.
- Use synthetic data in tests, examples, screenshots and browser fixtures: `.localhost`, `example.com`, `example.test`, and generic paths such as `/Users/me/project`. Never copy real Docker labels, project inventories, resolver contents, logs, or command output into tracked files, commits, PRs or public messages without sanitising them. Do not record actual private values in redaction lists or audit reports committed here.
- Before committing or publishing, inspect the complete diff (including staged/untracked files), documentation, CI, image references, fixtures and generated assets for infrastructure details and secrets. Check ignored files explicitly before adding anything; never force-add local configuration or `caddy_data/`.
- Before a first public mirror or a change to mirrored refs, inspect every included branch/tag's reachable history, commit messages and author/committer emails, annotated tags and Git LFS objects if present. Deleting a value in a new commit does not remove it from history. Report historical leaks privately and resolve the publication strategy with the user before publishing affected refs. Do not rewrite existing history or force-push without explicit authorization.
- Publish only reviewed branches/tags. Do not use `git push --mirror` from a development checkout: it can also publish private tooling/checkpoint refs. Do not copy `.git/config`, local hooks, ignored files, runtime state or Docker build artifacts into a public export.

## Code conventions

- Indent with tabs, not spaces, in new files and in code you change.
	- YAML (`compose.yml`, `.gitlab-ci.yml`) is the exception: tabs aren't valid there, so keep spaces.
	- Don't reindent existing files just to convert them; only touch lines you're changing.

- Python tests live in `tests/unit` (fast, everything faked) and `tests/integration` (real sockets, subprocesses or HTTP on localhost), with shared builders in `tests/support.py`. Run them with `just test`, or `just test unit` / `just test integration`. Put new tests in the matching group, and don't add tests under `dns/`, which is the Docker build context.

- The routes page's JavaScript and CSS live in `dns/static/` and are served from this origin. Don't add inline `<script>`, `<style>` or `style=` to the HTML: the page's Content-Security-Policy forbids them. Anything that comes from a container (names, labels, paths) must be HTML-escaped in `dns/web.py` and set as text, never HTML, in `dns/static/app.js`. The browser behaviour isn't covered by `just test`; check it by serving `dns/web.py` with fake Docker data and driving it in a browser.
