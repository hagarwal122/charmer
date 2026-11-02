"""Config checks. Run inside the pinned LiteLLM image: python -m unittest tests.test_config"""
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.test.yml"]
PINNED = re.compile(r"^[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")


class ComposeLoader(yaml.SafeLoader):
    """Compose's merge tags (!override, !reset) read as plain values."""


for _tag in ("!override", "!reset"):
    ComposeLoader.add_constructor(_tag, lambda loader, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else loader.construct_scalar(node)))


def compose_images():
    for name in COMPOSE_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        doc = yaml.load(path.read_text(), Loader=ComposeLoader)
        for svc_name, svc in (doc.get("services") or {}).items():
            if "image" in svc and "build" not in svc:  # a local build is checked through its Dockerfile's FROM
                yield name, svc_name, svc["image"]


class ImagePins(unittest.TestCase):
    def test_cfg02_every_image_pinned_by_digest(self):
        """CFG-02: no image uses latest, main-stable, or a tag without @sha256."""
        images = list(compose_images())
        self.assertTrue(images, "no images found in the compose files")
        for name, svc, image in images:
            with self.subTest(file=name, service=svc):
                self.assertRegex(image, PINNED, f"{svc} is not pinned by digest")
                self.assertNotRegex(image, r":(latest|main-stable)@", f"{svc} uses a floating tag")

    def test_cfg02_local_builds_start_from_a_pinned_base(self):
        """CFG-02: every Dockerfile's FROM is pinned by digest."""
        files = list(ROOT.glob("**/Dockerfile"))
        self.assertTrue(files)
        for f in files:
            for line in f.read_text().splitlines():
                if line.strip().upper().startswith("FROM "):
                    with self.subTest(file=str(f)):
                        self.assertRegex(line.split()[1], PINNED)

    def test_cfg02_never_the_malicious_release(self):
        """1.107.0 must never be referenced by anything that runs. The plan docs may name it as a warning."""
        runnable = [ROOT / "Makefile", *ROOT.glob("*.yml"), *ROOT.glob("*.yaml"), *ROOT.glob("scripts/*")]
        for path in runnable:
            if path.is_file():
                self.assertNotIn("1.107.0", path.read_text(errors="ignore"), str(path))

    def test_cfg01_litellm_pin_matches_cosign_input(self):
        """CFG-01 (static half): the LiteLLM image is the v1.106.4 tag with a digest; cosign.pub is vendored."""
        litellm = [img for _, svc, img in compose_images() if svc == "litellm"]
        self.assertEqual(len(litellm), 1)
        self.assertTrue(litellm[0].startswith("ghcr.io/berriai/litellm:v1.106.4@sha256:"))
        self.assertIn("BEGIN PUBLIC KEY", (ROOT / "cosign.pub").read_text())


if __name__ == "__main__":
    unittest.main()


# ---- step 2: config, compose and the free guard -------------------------------------------------------------
import importlib.util
import os
import re as _re
import subprocess
import sys
import tempfile
import zoneinfo


def load_yaml(name):
    return yaml.safe_load((ROOT / name).read_text())


def guard_module():
    spec = importlib.util.spec_from_file_location("free_guard", ROOT / "scripts" / "free_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def deployments(cfg):
    return cfg.get("model_list") or []


def chat_deployments(cfg):
    return [d for d in deployments(cfg) if (d.get("model_info") or {}).get("mode") != "embedding"]


def env_refs(obj):
    if isinstance(obj, str):
        return [obj.split("/", 1)[1]] if obj.startswith("os.environ/") else []
    if isinstance(obj, dict):
        return [r for v in obj.values() for r in env_refs(v)]
    if isinstance(obj, list):
        return [r for v in obj for r in env_refs(v)]
    return []


ALL_KEYS_SET = {name: "x" * 40 for name in env_refs(load_yaml("config.yaml"))} if (ROOT / "config.yaml").exists() else {}


class ConfigShape(unittest.TestCase):
    def setUp(self):
        self.cfg = load_yaml("config.yaml")

    def test_cfg04_secrets_only_from_env(self):
        """CFG-04: every api_key and api_base is os.environ/...; no literal secret or keyed URL."""
        for d in deployments(self.cfg):
            p = d["litellm_params"]
            for field in ("api_key", "api_base"):
                if field in p:
                    with self.subTest(id=d["model_info"]["id"], field=field):
                        self.assertTrue(str(p[field]).startswith("os.environ/"))

    def test_cfg05_general_settings(self):
        """CFG-05: no DB, custom auth with both model checks, no client credentials, no background health checks."""
        g = self.cfg["general_settings"]
        self.assertFalse(g["allow_client_side_credentials"])
        self.assertFalse(g["background_health_checks"])
        self.assertEqual(g["master_key"], "os.environ/LITELLM_MASTER_KEY")
        self.assertEqual(g["custom_auth"], "charmer_auth.user_api_key_auth")
        self.assertTrue(g["custom_auth_run_common_checks"])
        self.assertNotIn("database_url", g)
        self.assertTrue(self.cfg["litellm_settings"]["enable_post_custom_auth_checks"])

    def test_cfg06_router_and_callbacks(self):
        """CFG-06: session affinity only (never enforce_model_rate_limits), simple-shuffle, plugin after prometheus."""
        r = self.cfg["router_settings"]
        self.assertEqual(r["optional_pre_call_checks"], ["session_affinity"])
        self.assertEqual(r["routing_strategy"], "simple-shuffle")
        self.assertEqual(self.cfg["litellm_settings"]["callbacks"], ["prometheus", "charmer_plugin.instance"])

    def test_cfg07_chat_deployment_fields(self):
        """CFG-07: weight, order, timeout, stream_timeout, cooldown_time; never rpm or tpm."""
        for d in chat_deployments(self.cfg):
            p = d["litellm_params"]
            with self.subTest(id=d["model_info"]["id"]):
                for field in ("weight", "order", "timeout", "stream_timeout", "cooldown_time"):
                    self.assertIn(field, p)
                self.assertNotIn("rpm", p)
                self.assertNotIn("tpm", p)

    def test_cfg08_ids_and_twins(self):
        """CFG-08: explicit ids; every chat member has an identical pair- twin; slugs never start with pair-."""
        by_id = {d["model_info"]["id"]: d for d in deployments(self.cfg)}
        for d in chat_deployments(self.cfg):
            mid, name = d["model_info"]["id"], d["model_name"]
            if name == "chat":
                self.assertFalse(mid.startswith("pair-"))
                twin = by_id.get(f"pair-{mid}")
                self.assertIsNotNone(twin, f"{mid} has no pair- twin")
                self.assertEqual(twin["model_name"], f"pair-{mid}")
                self.assertEqual(twin["litellm_params"], d["litellm_params"])
            else:
                self.assertTrue(name.startswith("pair-"), f"{mid}: chat groups are chat or pair-*")
                self.assertEqual(mid, name)

    def test_cfg09_unique_ids_no_slash_groups(self):
        """CFG-09: ids unique; no group name contains '/'."""
        ids = [d["model_info"]["id"] for d in deployments(self.cfg)]
        self.assertEqual(len(ids), len(set(ids)))
        for d in deployments(self.cfg):
            self.assertNotIn("/", d["model_name"])

    def test_cfg10_cfg15_embedding_group(self):
        """CFG-10, CFG-15: embed-bge-m3 has one deployment, on @cf/baai/bge-m3; each embed-* group is one model."""
        emb = [d for d in deployments(self.cfg) if d["model_name"] == "embed-bge-m3"]
        self.assertEqual(len(emb), 1)
        self.assertTrue(emb[0]["litellm_params"]["model"].endswith("@cf/baai/bge-m3"))
        models = {}
        for d in deployments(self.cfg):
            if d["model_name"].startswith("embed-"):
                models.setdefault(d["model_name"], set()).add(d["litellm_params"]["model"])
        for group, ms in models.items():
            self.assertEqual(len(ms), 1, group)

    def test_cfg11_worst_case_under_90s(self):
        """CFG-11: on the rendered config, timeout x (1 + retries) < 90 s; one-member groups never retry."""
        rendered, _ = guard_module().render(self.cfg, ALL_KEYS_SET)
        retries = rendered["router_settings"]["num_retries"]
        policy = rendered["router_settings"].get("model_group_retry_policy") or {}
        groups = {}
        for d in deployments(rendered):
            groups.setdefault(d["model_name"], []).append(d)
        for group, members in groups.items():
            with self.subTest(group=group):
                if len(members) == 1:
                    self.assertIn(group, policy)
                    self.assertTrue(all(v == 0 for v in policy[group].values()))
                    n = 0
                else:
                    n = retries
                for d in members:
                    self.assertLess(d["litellm_params"]["timeout"] * (1 + n), 90)

    def test_cfg26_every_deployment_has_a_known_account(self):
        """CFG-26: model_info.account names an account in limits.yaml."""
        accounts = load_yaml("limits.yaml")["accounts"]
        for d in deployments(self.cfg):
            with self.subTest(id=d["model_info"]["id"]):
                self.assertIn(d["model_info"].get("account"), accounts)

    def test_cfg27_day_limits_have_a_time_zone(self):
        """CFG-27: every account or model with a day limit has a valid IANA day_tz (on itself or its account)."""
        limits = load_yaml("limits.yaml")
        for name, acct in limits["accounts"].items():
            if any(k.startswith("day_") and k != "day_tz" for k in acct):
                with self.subTest(account=name):
                    zoneinfo.ZoneInfo(acct["day_tz"])
        self.assertEqual(limits["accounts"]["gemini"]["day_tz"], "America/Los_Angeles")


class Compose(unittest.TestCase):
    def setUp(self):
        self.doc = load_yaml("docker-compose.yml")
        self.litellm = self.doc["services"]["litellm"]

    def test_cfg12_one_worker_memory_localhost_no_db(self):
        """CFG-12: one worker, mem_limit, 127.0.0.1 ports, no database service."""
        cmd = self.litellm["command"]
        self.assertEqual(cmd[cmd.index("--num_workers") + 1], "1")
        self.assertIn("mem_limit", self.litellm)
        for svc in self.doc["services"].values():
            for port in svc.get("ports", []):
                self.assertTrue(str(port).startswith("127.0.0.1:"), port)
        names = " ".join(self.doc["services"]).lower()
        for db in ("postgres", "redis", "mysql"):
            self.assertNotIn(db, names)
        self.assertNotIn("DATABASE_URL", str(self.litellm.get("environment", {})))

    def test_cfg22_rendered_mount_guard_first_gitignore(self):
        """CFG-22: compose mounts rendered/; up, restart and upgrade run the guard first; .gitignore covers it."""
        self.assertIn("./rendered:/app/rendered:ro", self.litellm["volumes"])
        self.assertIn("/app/rendered/config.yaml", self.litellm["command"])
        make = (ROOT / "Makefile").read_text()
        for target in ("up", "restart", "upgrade"):
            line = _re.search(rf"^{target}:([^\n]*)", make, _re.M)
            self.assertIsNotNone(line, target)
            self.assertIn("guard", line.group(1), f"make {target} must run the guard first")
        restart = _re.search(r"^restart:.*?(?=^\S)", make, _re.M | _re.S).group(0)
        self.assertIn("--force-recreate", restart, "restart must recreate the containers so .env changes reach them")
        self.assertIn("dashboard", restart, "the dashboard is recreated too, so its config files are fresh")
        ignored = (ROOT / ".gitignore").read_text().split()
        self.assertIn("rendered/", ignored)
        self.assertIn("data/", ignored)

    def test_cfg31_live_scripts_run_as_you(self):
        """CFG-31: the live checks write reports/ as the invoking user, like the guard's scripts; as root (the old
        LIVE) a Linux server could not update or remove its own reports."""
        make = (ROOT / "Makefile").read_text()
        live = _re.search(r"^LIVE := (.*)$", make, _re.M).group(1)
        self.assertIn("$(RUN_AS)", live)

    def test_cfg29_tunnel_is_locally_managed(self):
        """CFG-29: cloudflared runs a locally managed tunnel from the committed cloudflared/config.yml and a
        gitignored credentials file, as the invoking user; no dashboard token. Setup has its own Makefile targets."""
        cfd = self.doc["services"]["cloudflared"]
        cmd = cfd["command"]
        self.assertEqual(cmd[cmd.index("--config") + 1], "/etc/cloudflared/config.yml")
        self.assertEqual(cmd[-2:], ["run", "${CLOUDFLARE_TUNNEL_ID}"])
        self.assertIn("./cloudflared:/etc/cloudflared:ro", cfd["volumes"])
        self.assertNotIn("TUNNEL_TOKEN", str(cfd.get("environment", {})))
        self.assertIn("CHARMER_UID", cfd["user"])
        self.assertEqual(cfd["profiles"], ["tunnel"])
        conf = load_yaml("cloudflared/config.yml")
        self.assertEqual(conf["credentials-file"], "/etc/cloudflared/credentials.json")
        ignored = (ROOT / ".gitignore").read_text().split()
        self.assertIn("cloudflared/*.json", ignored)
        self.assertIn("cloudflared/cert.pem", ignored)
        makefile = (ROOT / "Makefile").read_text()
        for target in ("tunnel-login:", "tunnel-create:", "tunnel-route:", "test-tunnel:"):
            self.assertIn(target, makefile)

    def test_cfg30_dashboard_tunnel_is_its_own(self):
        """CFG-30: the dashboard, when published, has its own tunnel, config and credentials, off unless its id is set;
        `make down` stops it too, and tunnel-create can write a second credentials file."""
        cfd = self.doc["services"]["cloudflared-dashboard"]
        cmd = cfd["command"]
        self.assertEqual(cmd[cmd.index("--config") + 1], "/etc/cloudflared/dashboard.yml")
        self.assertEqual(cmd[-2:], ["run", "${CLOUDFLARE_DASHBOARD_TUNNEL_ID}"])
        self.assertIn("./cloudflared:/etc/cloudflared:ro", cfd["volumes"])
        self.assertIn("CHARMER_UID", cfd["user"])
        self.assertEqual(cfd["profiles"], ["dashboard-tunnel"])
        conf = load_yaml("cloudflared/dashboard.yml")
        self.assertEqual(conf["credentials-file"], "/etc/cloudflared/dashboard-credentials.json")
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn("--profile dashboard-tunnel", makefile)
        self.assertIn("CREDS", makefile)
        down = makefile[makefile.index("down:"):].split("\n\n")[0]
        self.assertIn("dashboard-tunnel", down)

    def test_cfg14_env_example_lists_every_variable(self):
        """CFG-14: .env.example names every os.environ/ in config.yaml and every ${VAR} in the compose files."""
        example = {l.split("=", 1)[0] for l in (ROOT / ".env.example").read_text().splitlines() if "=" in l and not l.startswith("#")}
        needed = set(env_refs(load_yaml("config.yaml")))
        for name in COMPOSE_FILES:
            if (ROOT / name).exists():
                needed |= set(_re.findall(r"\$\{([A-Z0-9_]+)", (ROOT / name).read_text()))
        self.assertEqual(sorted(needed - example), [])


class FreeGuard(unittest.TestCase):
    def setUp(self):
        self.g = guard_module()
        self.free = load_yaml("free-models.yaml")

    def dep(self, mid, model, account, group=None):
        return {"model_name": group or f"pair-{mid}", "model_info": {"id": mid, "account": account},
                "litellm_params": {"model": model, "api_key": f"os.environ/{account.upper()}_KEY"}}

    def cfg(self, *deps):
        return {"model_list": list(deps), "router_settings": {"num_retries": 2}}

    def test_cfg17_openrouter_must_be_free(self):
        """CFG-17: an openrouter model without :free is refused and named; the CLI exits non-zero."""
        bad = self.dep("or-paid", "openrouter/nvidia/nemotron-3.5-super-vl", "openrouter")
        errors = self.g.check(self.cfg(bad), self.free)
        self.assertTrue(any("or-paid" in e for e in errors))
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "c.yaml")
            path.write_text(yaml.safe_dump(self.cfg(bad)))
            out = pathlib.Path(tmp, "out.yaml")
            run = subprocess.run([sys.executable, str(ROOT / "scripts/free_guard.py"), "--config", str(path),
                                  "--free", str(ROOT / "free-models.yaml"), "--out", str(out)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 3, "a free-only refusal has its own exit code")
            self.assertIn("or-paid", run.stdout + run.stderr)
            self.assertFalse(out.exists(), "a refused config must not be rendered")
            out.write_text("stale: true\n")
            run = subprocess.run([sys.executable, str(ROOT / "scripts/free_guard.py"), "--config", str(path),
                                  "--free", str(ROOT / "free-models.yaml"), "--out", str(out)], capture_output=True, text=True)
            self.assertFalse(out.exists(), "a refusal removes the last rendered config, so a reboot cannot serve it")
        make = (ROOT / "Makefile").read_text()
        guard_line = _re.search(r"^guard:.*?(?=^\S)", make, _re.M | _re.S).group(0)
        self.assertIn("stop litellm", guard_line, "a refused config stops the running LiteLLM")
        self.assertIn("= 3", guard_line, "only a refusal (exit 3) stops LiteLLM; any other guard failure leaves it")
        pyrun = _re.search(r"^PYRUN := (.*)$", make, _re.M).group(1)
        self.assertIn("$(PYRUN)", guard_line)
        self.assertIn("$(COMPOSE) run", pyrun, "the guard reads .env the way compose does")
        for target in ("up", "restart", "upgrade"):
            body = _re.search(rf"^{target}:.*?(?=^\S)", make, _re.M | _re.S).group(0)
            self.assertIn("--build", body, f"make {target} rebuilds the dashboard image")
        self.assertRegex(_re.search(r"^test:.*$", make, _re.M).group(0), "test-boot")

    def test_cfg18_model_not_on_the_list(self):
        """CFG-18: any model not in free-models.yaml is refused, whatever the provider."""
        errors = self.g.check(self.cfg(self.dep("g-x", "gemini/gemini-9-pro", "gemini")), self.free)
        self.assertTrue(any("g-x" in e for e in errors))

    def test_cfg19_provider_rules_win_over_the_list(self):
        """CFG-19: mistral labs-/preview and any zai model but glm-4.7-flash are refused even if listed."""
        free = {**self.free, "mistral": [*self.free.get("mistral", []), {"model": "mistral/labs-x", "date": "d", "source": "s"},
                                          {"model": "mistral/foo-preview", "date": "d", "source": "s"}],
                "zai": [*self.free.get("zai", []), {"model": "zai/glm-5.3-flash", "date": "d", "source": "s"}]}
        cfg = self.cfg(self.dep("m1", "mistral/labs-x", "mistral"), self.dep("m2", "mistral/foo-preview", "mistral"),
                       self.dep("z1", "zai/glm-5.3-flash", "zai"))
        errors = " ".join(self.g.check(cfg, free))
        for mid in ("m1", "m2", "z1"):
            self.assertIn(mid, errors)

    def test_cfg20_real_config_passes_and_list_is_sourced(self):
        """CFG-20: the real config passes; every free-models entry has a date and a source."""
        self.assertEqual(self.g.check(load_yaml("config.yaml"), self.free), [])
        for account, entries in self.free.items():
            for e in entries:
                with self.subTest(account=account, model=e.get("model")):
                    self.assertTrue(e.get("date"))
                    self.assertTrue(e.get("source"))

    def test_cfg21_render_leaves_out_providers_without_a_key(self):
        """CFG-21: all keys set renders every deployment; GROQ_API_KEY empty removes both groq copies and nothing else."""
        cfg = load_yaml("config.yaml")
        full, left = self.g.render(cfg, ALL_KEYS_SET)
        self.assertEqual([d["model_info"]["id"] for d in deployments(full)], [d["model_info"]["id"] for d in deployments(cfg)])
        self.assertEqual(left, [])
        env = {**ALL_KEYS_SET, "GROQ_API_KEY": ""}
        part, left = self.g.render(cfg, env)
        groq = {d["model_info"]["id"] for d in deployments(cfg) if d["model_info"]["account"] == "groq"}
        self.assertTrue(groq)
        self.assertEqual({d["model_info"]["id"] for d in deployments(part)},
                         {d["model_info"]["id"] for d in deployments(cfg)} - groq)
        self.assertEqual({x["id"] for x in left}, groq)

    def test_guard_refuses_a_deployment_with_no_account(self):
        """a deployment without model_info.account is refused."""
        d = self.dep("noacct", "openrouter/x:free", "openrouter")
        del d["model_info"]["account"]
        self.assertTrue(any("noacct" in e for e in self.g.check(self.cfg(d), self.free)))


class RealConfig(unittest.TestCase):
    """Step 8: the real config.yaml and limits.yaml together (CFG-16, CFG-24, CFG-25)."""

    def setUp(self):
        self.cfg = load_yaml("config.yaml")
        self.limits = load_yaml("limits.yaml")

    def test_cfg16_groups_your_app_calls_exist(self):
        """CFG-16: chat and embed-bge-m3 exist from the first start: as members in config.yaml or declared in
        limits.yaml, so an app calling them gets busy (429), never 400."""
        known = {d["model_name"] for d in deployments(self.cfg)} | set(self.limits.get("groups") or [])
        self.assertLessEqual({"chat", "embed-bge-m3"}, known)

    def test_cfg24_every_deployment_has_limits_with_a_source(self):
        """CFG-24: every chat deployment's account and model have a limits entry, each with a source."""
        for d in chat_deployments(self.cfg):
            model, account = d["litellm_params"]["model"], d["model_info"]["account"]
            with self.subTest(id=d["model_info"]["id"]):
                self.assertIn(account, self.limits["accounts"])
                self.assertIn(model, self.limits["models"])
                self.assertTrue(self.limits["models"][model].get("source"))
                self.assertTrue(self.limits["accounts"][account].get("source"))

    def test_cfg25_left_out_providers_are_named(self):
        """CFG-25: with a provider key empty, the guard's output names each deployment it left out."""
        env = {**os.environ, **ALL_KEYS_SET, "GROQ_API_KEY": ""}
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run([sys.executable, str(ROOT / "scripts/free_guard.py"), "--config", str(ROOT / "config.yaml"),
                                  "--free", str(ROOT / "free-models.yaml"), "--out", os.path.join(tmp, "r.yaml")],
                                 capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("left out pair-groq-gpt-oss-120b (no GROQ_API_KEY)", run.stdout)
