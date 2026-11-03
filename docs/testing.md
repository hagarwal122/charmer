# Testing

Every suite runs in Docker through the Makefile. No test calls a real provider except the live checklist, which you run
by hand.

`make test` runs everything below except e2e and live. Run it before every push; there is no CI.

| Target | Cells | Where | Against |
| --- | --- | --- | --- |
| `make test-verify` | CFG-01 | `tests/verify_test.sh` | cosign, the pinned image, and a tampered pin |
| `make test-tunnel` | CFG-28 | `tests/tunnel_test.sh` | both tunnels' routes, by the pinned cloudflared offline |
| `make test-cfg` | CFG-02 to CFG-27, CFG-29 to CFG-31 | `tests/test_config.py` | the real config files and the Makefile, in the LiteLLM image |
| `make test-unit` | CFG-23, PLG-01 to PLG-16 | `tests/test_auth.py`, `tests/test_plugin.py` | the auth hook and the plugin, no proxy and no network |
| `make test-dashboard` | DSH-01 to DSH-10, DSH-13 to DSH-21 | `tests/test_dashboard.py` | the app in-process, a fixture SQLite file and a fake LiteLLM |
| `make test-boot` | CFG-03 | `tests/boot_check.py` | LiteLLM booted with the real config and placeholder keys; no provider is called |
| `make test-smoke` | SMK-01 to SMK-35, FREE, DSC, BKP-01 | `tests/smoke.py` | LiteLLM and the plugin against the stub provider, then a restart |
| `make test-e2e` | DSH-12 | `tests/e2e_dashboard.py` | a real Chromium (Playwright) on the stub stack; screenshots go to `reports/shots/` |
| `make live-check PAIR=…` | LIVE-01 to LIVE-16 | `scripts/live_check.py` | a real provider, one pair at a time, with `CHARMER_API_KEY` |

Each cell's docstring says what it checks. A cell asserts what SHOULD happen, not what the code happens to do.

## The stub provider

`tests/stub_provider.py` is a fake OpenAI-compatible provider. The model name picks its behaviour, for example:

- `ok-*` answers;
- `slow`, `err-500`, `rl-429`, `pay-402` and `hdr-*` exercise timeouts, errors, 429s, billing and
  rate-limit headers;
- `strict` and `strict-null` reject non-standard message fields, the way some real providers do.

It also serves:

- the providers' model-list endpoints, for discovery;
- a Gemini `:generateContent` probe;
- Cloudflare's GraphQL neuron count;
- `/_set` and `/_log`, so a test can change its behaviour and read what it received.

The smoke stack (`docker-compose.test.yml`) uses:

- `tests/config.test.yaml`, `tests/limits.test.yaml` and `tests/free-models.test.yaml`;
- fake keys from `tests/env.test`.

Single cells: `make smoke-up`, then `make smoke-run SMOKE_ARGS="-v tests.smoke.Step4"`, then `make smoke-down`.

## The live checklist

`make live-check PAIR=pair-<slug>` calls one real model through your running Charmer. Every call spends free quota, so
run it only when you are adding or re-checking a model. It checks:

| Cell | What it checks |
| --- | --- |
| LIVE-01, LIVE-02 | A plain answer, and a streamed one |
| LIVE-04 to LIVE-07 | Tool calls: single and parallel, streamed, and a full round trip |
| LIVE-08 | An agent-sized request: about 6K prompt tokens and 27 tools |
| LIVE-09 | The shape of the tool-call ids |
| LIVE-11 to LIVE-16 | Limits, headers and timing notes |

It saves the tool history for LIVE-10:

- `make live-matrix` replays every saved history to every other checked pair. This proves a conversation can move
  between models.
- `make live-headers PAIR=…` re-reads the provider headers that were seen, without making new calls.

