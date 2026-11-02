# Every Docker operation for Charmer goes through these targets (AGENTS.md).
SHELL := /bin/sh
LITELLM_IMAGE := $(shell sed -n 's/^ *image: *\(ghcr\.io\/berriai\/litellm[^ ]*\).*/\1/p' docker-compose.yml | head -1)
PY := docker run --rm --entrypoint python -v $(CURDIR):/w:ro -w /w $(LITELLM_IMAGE)
ENV_FILE ?= .env
COMPOSE := docker compose
# cloudflared reads its 600 credentials file as you (docker-compose.yml).
export CHARMER_UID := $(shell id -u)
export CHARMER_GID := $(shell id -g)
# The dashboard's tunnel starts with up-tunnel only once its id is in .env (docs/guide.md §7).
DASHBOARD_TUNNEL := $(if $(shell grep -s '^CLOUDFLARE_DASHBOARD_TUNNEL_ID=.' $(ENV_FILE)),--profile dashboard-tunnel)
CLOUDFLARED_IMAGE := $(shell sed -n 's/^ *image: *\(cloudflare\/cloudflared[^ ]*\).*/\1/p' docker-compose.yml | head -1)

.PHONY: wait-ready restore-drill restore-drill-down live-check live-headers live-matrix discover test-boot test-e2e free-check cf-budget pull unpull backup dashboard-image dashboard-lock test-dashboard key-new verify guard up up-tunnel tunnel-login tunnel-create tunnel-route test-tunnel restart upgrade down ps logs test test-verify test-cfg test-unit test-smoke smoke-up smoke-down smoke-run smoke-restart smoke-logs

verify:            ## cosign-verify the pinned LiteLLM image
	@scripts/verify-image.sh

RUN_AS := --user $$(id -u):$$(id -g)
# Scripts run through the litellm service so they read .env exactly as LiteLLM does (quotes included).
PYRUN := $(COMPOSE) run --rm --no-deps -T $(RUN_AS) --entrypoint python -v $(CURDIR):/w -w /w litellm

guard:             ## free-only check, then render rendered/config.yaml for the keys present; a refusal (exit 3) stops LiteLLM
	@mkdir -p rendered
	@$(PYRUN) scripts/free_guard.py; rc=$$?; \
	if [ $$rc = 3 ]; then $(COMPOSE) stop litellm >/dev/null 2>&1; echo "guard refused the config: LiteLLM stopped. Fix config.yaml, then make up"; exit 1; fi; \
	if [ $$rc != 0 ]; then echo "guard could not run (exit $$rc): nothing changed, LiteLLM left as it was"; exit 1; fi

up: verify guard   ## start LiteLLM and the dashboard without the tunnel; waits until LiteLLM answers
	@$(COMPOSE) up -d --build
	@$(MAKE) -s wait-ready

up-tunnel: verify guard
	@$(COMPOSE) --profile tunnel $(DASHBOARD_TUNNEL) up -d --build
	@$(MAKE) -s wait-ready

restart: guard     ## re-render, then recreate LiteLLM and the dashboard so .env and config changes reach them (nightly)
	@$(COMPOSE) up -d --build --force-recreate litellm dashboard
	@$(MAKE) -s wait-ready

wait-ready:        ## wait up to 2 minutes for LiteLLM's liveliness; show its log if it never answers
	@for i in $$(seq 60); do \
	  $(COMPOSE) exec -T litellm python -c "import urllib.request;urllib.request.urlopen('http://localhost:4000/health/liveliness',timeout=3)" >/dev/null 2>&1 && { echo "LiteLLM is up"; exit 0; }; \
	  sleep 2; done; echo "LiteLLM did not come up. Its last log lines:"; $(COMPOSE) logs --tail 40 litellm; exit 1

upgrade: verify guard
	@$(COMPOSE) pull litellm && $(COMPOSE) up -d --build
	@$(MAKE) -s wait-ready

# Tunnel setup, once per machine (docs/guide.md §7). cloudflared/ holds cert.pem and credentials.json, both gitignored.
CFD = docker run --rm $(RUN_AS) -e HOME=/h -v $(CURDIR)/cloudflared:/h/.cloudflared $(CLOUDFLARED_IMAGE)

tunnel-login:      ## once: prints a link; approve your Cloudflare zone in a browser. Writes cloudflared/cert.pem
	@$(CFD) tunnel login

tunnel-create:     ## once: NAME=<tunnel name> [CREDS=dashboard-credentials.json]. Writes cloudflared/$(CREDS); put the printed id in .env
	@test -n "$(NAME)" || { echo "usage: make tunnel-create NAME=<tunnel name> [CREDS=<file>]"; exit 1; }
	@$(CFD) tunnel create --credentials-file /h/.cloudflared/$(or $(CREDS),credentials.json) $(NAME)

tunnel-route:      ## NAME=<tunnel name> HOST=<hostname>: a proxied DNS record sending HOST to the tunnel
	@test -n "$(NAME)" -a -n "$(HOST)" || { echo "usage: make tunnel-route NAME=<tunnel name> HOST=<hostname>"; exit 1; }
	@$(CFD) tunnel route dns $(NAME) $(HOST)

down:
	@$(COMPOSE) --profile tunnel --profile dashboard-tunnel down

ps:
	@$(COMPOSE) ps

logs:
	@$(COMPOSE) logs --tail 200 litellm

key-new:           ## print a new key and its .env line once; writes nothing. NAME=CHARMER_API_KEY (default), LITELLM_MASTER_KEY, CHARMER_METRICS_KEY
	@docker run --rm --entrypoint python $(LITELLM_IMAGE) -c "import secrets,sys; print(sys.argv[1].upper()+'=sk-'+secrets.token_hex(24))" "$(or $(NAME),CHARMER_API_KEY)"

SCRIPT := $(COMPOSE) run --rm --no-deps -T --entrypoint python -v $(CURDIR)/scripts:/scripts litellm

LIVE := $(COMPOSE) run --rm --no-deps -T $(RUN_AS) --entrypoint python -v $(CURDIR):/w -w /w litellm

live-check:        ## the live checklist for one pair through the running local Charmer; PAIR=pair-<slug>
	@test -n "$(PAIR)" || { echo 'usage: make live-check PAIR=pair-<slug>'; exit 2; }
	@$(LIVE) scripts/live_check.py "$(PAIR)"

live-headers:      ## re-read LIVE-16 (provider headers seen) for PAIR without new calls
	@$(LIVE) scripts/live_check.py --headers "$(PAIR)"

live-matrix:       ## LIVE-10: every saved tool history replayed to every other checked pair
	@$(LIVE) scripts/live_check.py --matrix

restore-drill:     ## the restore drill: BACKUP=backups/charmer-<stamp>.sqlite into a scratch project on random ports
	@test -n "$(BACKUP)" || { echo 'usage: make restore-drill BACKUP=backups/charmer-<stamp>.sqlite'; exit 2; }
	@docker volume create charmer-drill_data >/dev/null
	@docker run --rm -v charmer-drill_data:/data -v $(CURDIR)/backups:/b:ro --entrypoint cp $(LITELLM_IMAGE) /b/$(notdir $(BACKUP)) /data/charmer.sqlite
	@CHARMER_LITELLM_PORT=0 CHARMER_DASHBOARD_PORT=0 $(COMPOSE) -p charmer-drill up -d litellm dashboard
	@echo "LiteLLM: $$($(COMPOSE) -p charmer-drill port litellm 4000)  dashboard: $$($(COMPOSE) -p charmer-drill port dashboard 8080)"
	@echo "Check the drill (docs/guide.md, Backups), then: make restore-drill-down"

restore-drill-down:
	@$(COMPOSE) -p charmer-drill down -v

discover:          ## weekly: which free models each provider lists, and which configured ones are gone; exit 1 on gone
	@mkdir -p reports
	@$(PYRUN) scripts/discover.py

free-check:        ## daily: Gemini 402 probe + the plugin's last OpenRouter reading; exit 1 mails the owner
	@$(SCRIPT) /scripts/free_check.py

cf-budget:         ## every 15 min: Cloudflare neurons from analytics; pulls and lifts Cloudflare deployments
	@$(SCRIPT) /scripts/cf_budget.py

pull:              ## make pull ID=<deployment id or provider/> REASON="..."  (no restart)
	@test -n "$(ID)" || { echo 'usage: make pull ID=<id> REASON=...'; exit 2; }
	@$(SCRIPT) /scripts/pulls.py pull "$(ID)" "$(REASON)"

unpull:            ## make unpull ID=<deployment id or provider/>
	@test -n "$(ID)" || { echo 'usage: make unpull ID=<id>'; exit 2; }
	@$(SCRIPT) /scripts/pulls.py unpull "$(ID)"

backup:            ## nightly: SQLite online backup into backups/, keep 14
	@mkdir -p backups
	@$(COMPOSE) run --rm --no-deps -T --entrypoint python -v $(CURDIR)/scripts:/scripts -v $(CURDIR)/backups:/backups litellm /scripts/backup.py

test-verify:       ## CFG-01: verify passes, and fails on a tampered or bare pin
	@sh tests/verify_test.sh

test-tunnel:       ## CFG-28: the tunnel's routes, checked offline by the pinned cloudflared
	@sh tests/tunnel_test.sh

test-cfg:          ## Config checks
	@$(PY) -m unittest tests.test_config

test-unit:         ## Unit cells: auth hook, plugin
	@$(PY) -m unittest tests.test_auth tests.test_plugin

TEST_COMPOSE := $(COMPOSE) -p charmer-test -f docker-compose.yml -f docker-compose.test.yml
SMOKE_ARGS ?= -v $(SMOKE_SUITE)

smoke-up:          ## the stub stack: render the stub pool, start LiteLLM + stub
	@mkdir -p rendered-test reports
	@docker run --rm --entrypoint python --env-file tests/env.test -v $(CURDIR):/w -w /w $(LITELLM_IMAGE) \
	  scripts/free_guard.py --config tests/config.test.yaml --free tests/free-models.test.yaml --out rendered-test/config.yaml
	@$(TEST_COMPOSE) up -d --build --force-recreate litellm stub dashboard

smoke-run:         ## run smoke cells on the running stub stack (make smoke-run SMOKE_ARGS=...)
	@$(TEST_COMPOSE) --profile smoke run --rm smoke $(SMOKE_ARGS)

smoke-restart:
	@$(TEST_COMPOSE) restart litellm >/dev/null && sleep 20

smoke-logs:
	@$(TEST_COMPOSE) logs --tail 300 litellm

smoke-down:
	@$(TEST_COMPOSE) --profile smoke down -v --remove-orphans

SMOKE_SUITE := tests.smoke.Step3 tests.smoke.Step4 tests.smoke.Step5AHistory tests.smoke.Step5Plugin tests.smoke.Step5ZFree tests.smoke.Step7Scripts tests.smoke.Step9Discovery

test-smoke:        ## SMK cells against the stub, then the restart cells; torn down whatever the result
	@$(MAKE) -s smoke-up; \
	$(TEST_COMPOSE) --profile smoke run --rm smoke $(SMOKE_ARGS); rc=$$?; \
	if [ $$rc = 0 ] && [ "$(SMOKE_ARGS)" = "-v $(SMOKE_SUITE)" ]; then \
	  $(TEST_COMPOSE) --profile smoke run --rm smoke -v tests.smoke.RestartA && \
	  $(TEST_COMPOSE) restart litellm >/dev/null && sleep 20 && \
	  $(TEST_COMPOSE) --profile smoke run --rm smoke -v tests.smoke.RestartB; rc=$$?; \
	  $(TEST_COMPOSE) logs litellm 2>&1 | grep -q CANARY-SMOKE && { echo "SMK-18: a prompt reached the logs"; rc=1; }; \
	fi; \
	[ $$rc = 0 ] || $(TEST_COMPOSE) logs --tail 80 litellm > reports/smoke-litellm.log 2>&1; \
	$(MAKE) -s smoke-down; exit $$rc

dashboard-image:   ## build the dashboard image
	@docker build -q -f dashboard/Dockerfile -t charmer-dashboard:local . >/dev/null

dashboard-lock:    ## regenerate dashboard/requirements.txt with hashes for arm64 and amd64
	@sh scripts/lock-dashboard.sh

test-dashboard: dashboard-image   ## DSH cells, in the dashboard image
	@docker run --rm -v $(CURDIR):/w:ro -w /w --entrypoint python charmer-dashboard:local -m unittest tests.test_dashboard

test-e2e:          ## live dashboard e2e in a real Chromium against the stub stack (DSH-12); screenshots in reports/shots
	@docker build -q -f tests/e2e.Dockerfile -t charmer-e2e:dev tests >/dev/null
	@$(MAKE) -s smoke-up; sleep 12; \
	docker run --rm --network charmer-test_default --env-file tests/env.test -e DASHBOARD=http://dashboard:8080 \
	  -e LITELLM=http://litellm:4000 -v $(CURDIR):/w -w /w charmer-e2e:dev python tests/e2e_dashboard.py; rc=$$?; \
	$(MAKE) -s smoke-down; exit $$rc

test-boot:         ## CFG-03 on the real config: boots with placeholder keys, lists the groups, no provider called
	@mkdir -p rendered-boot
	@docker run --rm --entrypoint python --env-file tests/env.boot -v $(CURDIR):/w -w /w $(LITELLM_IMAGE) \
	  scripts/free_guard.py --out rendered-boot/config.yaml >/dev/null
	@docker network create charmer-boot >/dev/null 2>&1 || true
	@docker run -d --rm --name charmer-boot-litellm --network charmer-boot --env-file tests/env.boot \
	  -e PYTHONPATH=/app/plugin -e CHARMER_DB=/tmp/charmer.sqlite -e DISABLE_ADMIN_UI=True -e LITELLM_MODE=PRODUCTION \
	  -e LITELLM_LOCAL_MODEL_COST_MAP=True -e CHARMER_CONFIG=/app/conf/config.yaml -e CHARMER_LIMITS=/app/conf/limits.yaml \
	  -v $(CURDIR)/rendered-boot:/app/rendered:ro -v $(CURDIR)/plugin:/app/plugin:ro \
	  -v $(CURDIR)/config.yaml:/app/conf/config.yaml:ro -v $(CURDIR)/limits.yaml:/app/conf/limits.yaml:ro \
	  $(LITELLM_IMAGE) --config /app/rendered/config.yaml --port 4000 --num_workers 1 >/dev/null
	@docker run --rm --network charmer-boot --env-file tests/env.boot -v $(CURDIR):/w:ro -w /w --entrypoint python \
	  $(LITELLM_IMAGE) tests/boot_check.py; rc=$$?; \
	docker rm -f charmer-boot-litellm >/dev/null 2>&1; docker network rm charmer-boot >/dev/null 2>&1; exit $$rc

test: test-verify test-tunnel test-cfg test-unit test-dashboard test-boot test-smoke
