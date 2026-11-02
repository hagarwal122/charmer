# Self-hosting guide

Everything here runs through `make`. Nothing is installed on the host except Docker and `make`.

## 1. Settings

Copy `.env.example` to `.env`. Every variable is listed there.

| Variable | What it is |
| --- | --- |
| `LITELLM_MASTER_KEY` | The admin key, at least 48 characters. Charmer refuses to start without it. It is refused through the tunnel, so it works only on the machine itself. |
| `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD` | The dashboard's one sign-in. Both required; the password 16+ characters. Change them here, then `make restart`. |
| `DASHBOARD_SESSION_SECRET` | The secret that signs the dashboard's session cookie. |
| `CHARMER_METRICS_KEY` | A key that can read `/metrics` and nothing else. The dashboard uses it. |
| `ALERT_WEBHOOK_URL` | Optional. Each alert is POSTed here as JSON, at most once an hour per kind. |
| `CHARMER_API_KEY` | The one key your app sends, at least 32 characters. Put the same value in your app's own environment. `make key-new` prints a fresh one. |
| Provider keys | `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`, `NVIDIA_NIM_API_KEY`, and Cloudflare's `CF_API_TOKEN` / `CF_AI_API_BASE`. Leave one empty and its models are left out. |
| `CF_ACCOUNT_ID`, `CF_ANALYTICS_TOKEN` | Optional. Let `make cf-budget` read today's Cloudflare neuron count. |
| `CHARMER_DASHBOARD_TZ` | The time zone of the dashboard's clocks. Defaults to UTC. |
| `CHARMER_LITELLM_PORT`, `CHARMER_DASHBOARD_PORT` | Host ports. Both bind to 127.0.0.1 only. |
| `CLOUDFLARE_TUNNEL_ID` | Optional. The tunnel `make tunnel-create` made (§7). Only `make up-tunnel` uses it. |
| `CLOUDFLARE_DASHBOARD_TUNNEL_ID` | Optional. The dashboard's own tunnel (§7). Empty keeps the dashboard unpublished. |
| `CHARMER_UID`, `CHARMER_GID` | Leave empty: the Makefile sets them to your user, so cloudflared can read its credentials. |
| `DASHBOARD_TRUST_PROXY` | Set to 1 only when your own HTTPS proxy, not cloudflared, sits in front of the dashboard. It marks the session cookie Secure. |

Use one free account per provider, with billing switched off. Charmer's limits assume each account's free tier and
nothing more.

## 2. Groups and the key

Your app calls a **group**, never a single model:

- `chat` holds the models you have approved for production. It starts empty.
- `embed-bge-m3` serves embeddings.
- Each model also has its own `pair-<slug>` group. Your evals and `make live-check` use it to test one model at a time.

`CHARMER_API_KEY` may call every group. A model name that is not a group gets a 400. Groups that start empty, such
as `chat`, are declared under `groups` in `limits.yaml`, so calling them answers busy (429) rather than 400.

To change the key, set the new value in `.env` and in your app, then run `make restart`.

## 3. Start, stop, restart

| Command | What it does |
| --- | --- |
| `make up` | Verifies the LiteLLM image, runs the free-only guard, starts LiteLLM and the dashboard, and waits until LiteLLM answers. |
| `make restart` | Re-renders the config and recreates both containers, so changes to `.env` and the config files take effect. Run it nightly. It also clears cooldowns. |
| `make down`, `make ps`, `make logs` | Stop the stack, list its containers, and follow its logs. |

The guard (`make guard`, which `up` and `restart` run first) does two things:

- **It refuses a config with any model that is not in `free-models.yaml`.** A refusal stops LiteLLM and removes the
  last rendered config.
- **It renders `rendered/config.yaml`.** This is `config.yaml` minus the deployments whose provider key is empty.

## 4. Adding a model

1. **Check that the model is free.** Use the provider's pricing page, with billing off. Add it to `free-models.yaml`
   with the date and your source.
2. **Add its limits to `limits.yaml`.** Add per-minute and per-day requests and tokens, the time zone its day resets in,
   and a source. If you have not yet seen a number in the provider's console, mark it `(U)`.
3. **Add a deployment to `config.yaml`.**
   - Give it an explicit `model_info.id` (its slug) and `model_info.account` (the provider).
   - Put it in the group `pair-<slug>`.
4. **Apply the change.** Run `make restart`, then `make live-check PAIR=pair-<slug>` and `make live-matrix`.
5. **Admit it to `chat`** once both pass. Add a twin deployment with the same parameters, in the group `chat`, with the id `<slug>`.
   Then run `make restart`.

To see what each provider lists today, run `make discover` (weekly is enough). It writes a report to `reports/`, and the
dashboard's Providers page shows it. Discovery only lists models: it never calls one, and never changes the pool.

## 5. Taking a model out without a restart

```sh
make pull ID=or-gemma-4-31b REASON="answers in the wrong language"   # one deployment
make pull ID=gemini/ REASON="billing alert"                            # a whole provider
make unpull ID=or-gemma-4-31b
```

The plugin picks up the change within about 2 seconds. The dashboard's Pairs page has the same Pull and Unpull buttons.

Some pulls are made by the free-plan guards (`who: guard`). These can be lifted only with `make unpull`, after you have
fixed the cause.

## 6. Cron

| When | Command | Why |
| --- | --- | --- |
| Nightly | `make restart` | Fresh render, cleared cooldowns. |
| Nightly | `make backup` | SQLite online backup into `backups/`. The newest 14 are kept. |
| Daily | `make free-check` | Probes Gemini for a 402, which means billing got attached, and pulls `gemini/`. Also checks the plugin's last reading of the OpenRouter key: money spent, credits, and free requests left. |
| Every 15 min | `make cf-budget` | Reads today's Cloudflare neurons. Pulls Cloudflare chat at the `chat_stop_at` limit and embeddings at `embed_stop_at`, and lifts both the next day. It does nothing when you have no Cloudflare deployment or analytics token. |
| Weekly | `make discover` | New free models, and configured models that have gone. |

Each script exits 1 when you should look. Let cron mail you the output (`MAILTO`), and set `ALERT_WEBHOOK_URL` for
push alerts.

## 7. Reaching it from outside

LiteLLM and the dashboard listen on 127.0.0.1 only. To let your app reach LiteLLM from the internet, use a
Cloudflare Tunnel. It is free on any Cloudflare account with your domain on it, needs no Zero Trust plan or card, and
opens no port on your router. The tunnel is managed from this repo: its routes are `cloudflared/config.yml`.

1. `make tunnel-login`. It prints a link: open it, sign in to Cloudflare and pick your domain. This writes
   `cloudflared/cert.pem`.
2. `make tunnel-create NAME=charmer`. This writes `cloudflared/credentials.json` and prints the tunnel's id. Put the id
   in `.env` as `CLOUDFLARE_TUNNEL_ID`.
3. `make tunnel-route NAME=charmer HOST=charmer.example.com`. This adds a proxied DNS record for that name.
4. `make up-tunnel`.

`cert.pem` and `credentials.json` are gitignored. Keep both out of backups that leave the machine; `cert.pem` can create
tunnels and DNS records in your zone, so delete it once setup is done and run `make tunnel-login` again when you need it.

Only `/v1/chat/completions`, `/v1/embeddings`, `/v1/models` and `/health/liveliness` reach LiteLLM through the
tunnel; everything else is a 404 at Cloudflare (`make test-tunnel` checks this). Through the tunnel, the admin key and
the metrics key are refused as well: only `CHARMER_API_KEY` works.

By default the dashboard is not published. Reach it over SSH, from any machine that can reach the server (a VPN such
as Tailscale works): `ssh -L 8480:127.0.0.1:8480 you@server`, then open `http://localhost:8480`. Use your
`CHARMER_DASHBOARD_PORT`.

To publish it on its own hostname, give it its own tunnel, so the inference tunnel's routes stay as they are:

1. `make tunnel-create NAME=charmer-dashboard CREDS=dashboard-credentials.json`. Put the printed id in `.env` as
   `CLOUDFLARE_DASHBOARD_TUNNEL_ID`.
2. `make tunnel-route NAME=charmer-dashboard HOST=charmer-dashboard.example.com`.
3. `make up-tunnel` starts it with the inference tunnel whenever that id is set.

Its routes are `cloudflared/dashboard.yml`: everything goes to the dashboard, never to LiteLLM. The dashboard is then
on the internet behind its own lock only: `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` (keep the password long and
random; `openssl rand -base64 36` gives 48 characters), five tries a minute per visitor, a Secure session cookie and a
CSRF token. Put Cloudflare Access in front
of it too if your plan allows. To take it off the internet, empty `CLOUDFLARE_DASHBOARD_TUNNEL_ID` and run
`make down` then `make up-tunnel`.

## 8. Backups and the restore drill

`make backup` writes `backups/charmer-<stamp>.sqlite`. Run a restore drill at least once a quarter:

```sh
make restore-drill BACKUP=backups/charmer-<stamp>.sqlite
```

The drill starts a scratch copy of the stack on random ports and prints them. Then check two things:

- The dashboard shows yesterday's day counts and open pulls.
- A call with `CHARMER_API_KEY` is answered.

Then run `make restore-drill-down`.

## 9. Upgrading LiteLLM

1. Pick the new release.
2. Update the digest in `docker-compose.yml` and the `LITELLM_IMAGE` line in the Makefile.
3. Run `make verify`. It cosign-checks the image against `cosign.pub`.
4. Run `make test`. The smoke suite covers the LiteLLM behaviours that Charmer relies on.
5. Run `make upgrade`.

Never use LiteLLM 1.107.0: it is a known malicious release.

## 10. The consumer contract

What your app can rely on:

- **Endpoints.** `POST /v1/chat/completions` and `POST /v1/embeddings` with `CHARMER_API_KEY`. Streaming and tool calls
  work.
- **Busy.** HTTP 429 with "No deployments available" means every free model in the group is out for now. Retry later;
  Charmer never answers with a paid model.
- **Sessions.** Send `x-litellm-session-id: <conversation id>` to keep a conversation on one model. Do not send
  `x-litellm-trace-id`: Charmer pins on the session id only.
- **Which model answered.** The response header `x-litellm-model-id` names the deployment that served the call.
- **Refused fields.** The body may carry only the standard OpenAI fields. Routing fields such as `api_base`, `api_key`,
  `extra_body`, `models`, `route` and `transforms` get a 400 that names the field.
- **Replayed history.** You may replay assistant turns that another provider produced. Charmer reduces each turn to the
  standard fields before it reaches the serving provider. Tool call ids are passed through unchanged.
- **Errors.** An unknown key gets 401, and a model name that is not a group gets 400. Neither counts against any
  pool.
