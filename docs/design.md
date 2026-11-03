# Design

Charmer is stock LiteLLM Proxy plus four small pieces:

- a free-only guard that renders the config;
- a custom auth hook;
- one router plugin;
- a dashboard.

State is LiteLLM's memory plus one SQLite file.

```
your app ──► LiteLLM Proxy (1 worker) ──► provider free tiers
              ├─ charmer_auth   .env: the admin, metrics and API keys
              ├─ charmer_plugin limits, pulls, sessions, history fitting
              └─ store.py ──► /data/charmer.sqlite ◄── dashboard (FastAPI) and the cron scripts
```

## Files you edit

| File | Holds |
| --- | --- |
| `config.yaml` | The pool, in LiteLLM's own format. Each deployment has an explicit `model_info.id` and `model_info.account`. Groups are `chat`, `embed-bge-m3` and one `pair-<slug>` per model. |
| `limits.yaml` | Every number the plugin enforces, per account and per model, each with a source; and `groups`, the groups that exist before they have a member (`chat`). `config.yaml` carries no rpm or tpm. |
| `free-models.yaml` | The allow-list. A model is in the pool only if it is here. |

## The guard

`scripts/free_guard.py` runs before every start.

1. **It refuses a config with any model missing from `free-models.yaml`.** For OpenRouter it also needs the `:free`
   suffix. A refusal exits 3 and removes `rendered/config.yaml`, so a reboot cannot serve a refused config. Make then
   stops LiteLLM.
2. **Otherwise it renders `rendered/config.yaml`.** This is the config minus the deployments whose provider key is
   empty. Groups with one member get no retries, which keeps the client's wait bounded.

## Auth

`plugin/charmer_auth.py` is LiteLLM's `custom_auth`, with LiteLLM's common checks still on. The checks run in this
order:

1. **The admin key** (`LITELLM_MASTER_KEY`, at least 48 characters) is the proxy admin. It is refused when the request
   came through Cloudflare (`cf-connecting-ip` is set).
2. **The metrics key** reads `/metrics` only. It too is refused through the tunnel.
3. **`CHARMER_API_KEY`**, the one key for your app, reaches the inference paths only. It may call every group; the
   plugin refuses a name that is not one.
4. **Anything else** gets 401 "charmer: unknown key".

A missing or short `CHARMER_API_KEY` (under 32 characters), one equal to the admin or metrics key, or a master key
under 48 characters stops the start.

## The plugin

`plugin/charmer_plugin.py` is a LiteLLM `CustomLogger`. It runs before LiteLLM's own session affinity, and LiteLLM
runs one worker so that its counters are the only ones.

### Before the call

- **Body check.** Only standard OpenAI fields pass, plus LiteLLM's own session and trace ids. A field that would
  re-route the call, or carry the caller's own credentials or headers, gets a 400 naming it. So does a tool that is not
  a `function`.
- **Unknown model.** A model name that is not a known group also gets a 400. A deployment's own name would skip the
  filter, so only group names route.

### Choosing a deployment

`async_filter_deployments` gets the group's healthy deployments and returns the ones that may serve.

**Hard filters drop a deployment whatever else happens:**

- it is pulled (by the owner, the dashboard, or a guard);
- its account's or model's day requests or day tokens are used up, less a margin. Each account's day resets in its
  own time zone (`day_tz`);
- the Cloudflare account has reached `chat_stop_at` or `embed_stop_at` neurons;
- an eval call (`pair-*` group) has used the account's `eval_day_requests` share;
- OpenRouter's last key reading says its free requests are used up today.

**Soft filters drop a deployment unless that would leave none:**

- the deployment already failed this same call (LiteLLM may otherwise pick it again for the retry);
- the account's or model's minute requests or minute tokens are full;
- the provider's own rate-limit headers say it is out until its reset.

**Sessions.** A call with `x-litellm-session-id` stays on the deployment that last served that session, while that
deployment passes. Otherwise a rendezvous hash picks among the lowest `order` (order 2 is overflow, used only when every
order-1 member is out).

**Busy.** An empty result means LiteLLM answers 429 "No deployments available". The plugin counts busy answers, and
alerts when a group goes dry.

**When the filter fails.** If the filter itself throws, the plugin alerts and keeps only the hard filters. A hard
check that throws drops its deployment: it fails closed.

### Counting

- **At dispatch.** Every call is counted against its account and model before it is sent, with an estimate of its
  tokens. This stops parallel calls from all passing the same limit. When the answer comes back, the real usage
  replaces the estimate.
- **Provider headers.** Remaining requests, remaining tokens, and reset or retry-after are read from each answer's
  headers, including Groq's and Mistral's names. Cloudflare's neurons come from its `cf-ai-neurons` header; without
  it, they are estimated from tokens (`neurons_per_1k_tokens`).
- **OpenRouter.** The plugin polls `GET /api/v1/key`. It alerts if money moved or credits changed, and it stops
  OpenRouter for the day when free requests run out.

### History fitting

Providers reject each other's extra assistant-message fields, such as `reasoning_content`, `provider_specific_fields`
or `index`. Before a call is sent, `fit_history` reduces every message to the standard fields. It keeps Gemini's own
fields when Gemini serves, and sends null content as `""` to Cloudflare. Tool call ids are never changed.

## The SQLite file

`plugin/store.py` is the standard library only. It runs in WAL mode with one writer thread and a busy timeout.

| Table | Holds |
| --- | --- |
| `usage_minute`, `usage_day`, `key_usage_minute` | Counts per account and model, and busy answers. Day counts survive a restart: the plugin loads them at start, and flushes on SIGTERM. |
| `provider_state` | The last headers seen per deployment. |
| `openrouter_key` | Each OpenRouter key reading. |
| `recent_calls` | The Calls page. Sessions are stored only as hashes. |
| `pulls` | One open row per (target, who). A guard's pull is never hidden by an owner's. |
| `alerts` | At most one an hour per kind, each also POSTed to `ALERT_WEBHOOK_URL`. |

Day rows and lifted pulls are pruned after 90 days.

## The dashboard

The dashboard is a FastAPI app with server-rendered Jinja templates and no build step. `static/app.js` refreshes the
numbers on Now from `/api/state` every 5 seconds, and draws the sparklines and the hourly chart as inline SVG. The
other pages show when they were rendered ("as of"). Hours and "today" follow `CHARMER_DASHBOARD_TZ`, never the
viewer's clock. Asset URLs carry a hash of the static files, so an upgrade never leaves a browser on an old script.

| Page | Shows |
| --- | --- |
| Now | KPIs, requests by pair (the five busiest, the rest as Other), each consumer group's pairs, the eval pairs in one card, LiteLLM's health |
| Calls | The last 1,000 calls, 50 a page, filtered by key, group, pair or status, with the success share and median latency |
| Providers | Accounts, their models, and the latest discovery report |
| Pairs | Each deployment's state (serving, near limit, filtered, cooling, pulled, no key), with Pull and Unpull; filters by group or "needs attention" |
| Alerts | Open alerts, with Clear |
| Brand | The design tokens |

Access:

- One password and an HMAC-signed session cookie (SameSite Lax).
- A CSRF token on every form, and a login throttle.
- The dashboard holds no provider key and no `CHARMER_API_KEY`. It reads LiteLLM's `/metrics` with the metrics key.

Every colour, size, radius and motion value lives in `static/tokens.css`, for both the dark and the light theme.

## Known exposure

LiteLLM serves `/public/litellm_model_cost_map` without auth. It reveals which deployment ids exist, but no keys or
secrets. The other public routes are switched off: the admin UI and the OpenAPI docs.
