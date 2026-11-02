# Working in the Charmer repo

Charmer is an open-source (MIT), self-hosted pool of free LLM tiers. It is LiteLLM Proxy, configured, plus a custom
auth hook, one filter plugin and a dashboard. Read `docs/design.md` before changing routing, limits or the guard.

Keep the code boringly easy to read. Prefer small single-purpose files, the standard library first, and no clever
abstractions.

## Golden rules

1. **Free only.**
   - Never add a paid model, tier or fallback.
   - Never enable billing, auto top-up or pay-as-you-go on any provider.
   - A model goes into `config.yaml` only if it is in `free-models.yaml`, with a date and a source.
2. **Never print a key.**
   - Provider keys, `CHARMER_API_KEY`, the admin key and passwords live only in `.env`, which is gitignored.
   - Check a key as "set (len N)" or by a short fingerprint, never by its value.
   - No key in logs, test output, commits or chat.
3. **Don't spend provider quota.**
   - Tests run against the stub provider (`tests/stub_provider.py`).
   - Live calls (`make live-check`) are run by hand, one pair at a time.
4. **Docker only, all through the Makefile.**
   - Every test, script, render and service runs in a container started by a Makefile target.
   - Never install a package on the host: no pip, npm or brew.
   - No raw `docker compose`. Add a target if one is missing.
5. **Pinned by digest.**
   - LiteLLM and every other image are pinned by digest. Never use a bare tag, `latest` or `main-stable`.
   - `make verify` cosign-checks LiteLLM. Never use LiteLLM 1.107.0: it is a known malicious release.
6. **One worker, no database server, no Redis.** State is LiteLLM's memory plus one SQLite file (`plugin/store.py`).
7. **The UI must look modern and attractive.**
   - Pages use only the tokens in `dashboard/static/tokens.css`; no colour or size literals anywhere else.
   - Every page is checked in light and dark, on desktop and phone (`make test-e2e` takes the screenshots).
   - A UI change is not done until it meets this bar.

## Making a change

1. Write the test cells first and see them fail. The cell IDs (CFG, PLG, SMK, FREE, DSC, BKP, DSH, LIVE) are listed in
   `docs/testing.md`.
2. Implement the smallest thing that passes. Use the standard library before adding a dependency, and reuse what
   LiteLLM already does.
3. Run the change's own suite, then `make test`.
   - Send the output to a log file.
   - Read back only the summary and the first failures.
4. A test asserts what SHOULD happen. Never soften or skip a failing cell to get green: fix the code, or say why.
5. Update the docs in the same commit when behaviour changes.

The plugin and the auth hook run inside the LiteLLM image (Python 3.13). Test code uses only `unittest`, `urllib`,
`json` and `http.server`. The dashboard's packages are pinned with hashes; `make dashboard-lock` regenerates them.

## Git

- Use Conventional Commits, for example `feat(plugin): …` or `fix(dashboard): …`.
- Never commit any of these:
  - `.env`;
  - `data/`, `backups/` or `reports/`;
  - the `rendered*/` directories.
