"""The free-only start check.

Refuses a config with any non-free model, then renders rendered/config.yaml: config.yaml minus the deployments whose
provider key is empty, with no retries for one-member groups. Exit 3 on a refusal (nothing rendered, the last rendered config removed); exit 1 if the guard itself fails.

Usage: python scripts/free_guard.py [--config config.yaml] [--free free-models.yaml] [--out rendered/config.yaml]
"""
import argparse
import copy
import os
import pathlib
import sys

import yaml

NO_RETRIES = {name: 0 for name in (
    "BadRequestErrorRetries", "AuthenticationErrorRetries", "TimeoutErrorRetries", "RateLimitErrorRetries",
    "ContentPolicyViolationErrorRetries", "InternalServerErrorRetries", "ServiceUnavailableErrorRetries")}


def _env_refs(value):
    if isinstance(value, str):
        return [value.split("/", 1)[1]] if value.startswith("os.environ/") else []
    if isinstance(value, dict):
        return [r for v in value.values() for r in _env_refs(v)]
    if isinstance(value, list):
        return [r for v in value for r in _env_refs(v)]
    return []


def _provider_rule(account, model):
    """Rules that win over the allow-list, keyed on the account (never the route prefix)."""
    if account == "openrouter" and not model.endswith(":free"):
        return "an OpenRouter model must end with :free"
    if account == "mistral" and ("labs-" in model or "preview" in model):
        return "Mistral labs- and preview models are always trained on and never free here"
    if account == "zai" and model != "zai/glm-4.7-flash":
        return "on Z.ai only glm-4.7-flash is free (glm-5.3-flash ends -flash and still bills)"
    return None


def check(cfg, free):
    """Every reason the config is not free-only. Empty list: it passes."""
    allowed = {account: {str(e["model"]) for e in entries or []} for account, entries in (free or {}).items()}
    errors = []
    for d in cfg.get("model_list") or []:
        info = d.get("model_info") or {}
        mid = info.get("id", "<no id>")
        model = str((d.get("litellm_params") or {}).get("model", ""))
        account = info.get("account")
        if not account:
            errors.append(f"{mid}: no model_info.account")
            continue
        rule = _provider_rule(account, model)
        if rule:
            errors.append(f"{mid}: {model}: {rule}")
        elif model not in allowed.get(account, set()):
            errors.append(f"{mid}: {model} is not in free-models.yaml under {account}")
    return errors


def render(cfg, env):
    """(rendered config, left-out deployments). Drops deployments whose os.environ/ values are empty."""
    out = copy.deepcopy(cfg)
    out = {k: v for k, v in out.items() if not str(k).startswith("x-")}
    kept, left = [], []
    for d in cfg.get("model_list") or []:
        missing = [name for name in _env_refs(d.get("litellm_params")) if not env.get(name)]
        if missing:
            left.append({"id": d["model_info"]["id"], "missing": missing})
        else:
            kept.append(copy.deepcopy(d))
    out["model_list"] = kept
    sizes = {}
    for d in kept:
        sizes[d["model_name"]] = sizes.get(d["model_name"], 0) + 1
    router = out.setdefault("router_settings", {})
    router["model_group_retry_policy"] = {g: dict(NO_RETRIES) for g, n in sorted(sizes.items()) if n == 1}
    return out, left


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--free", default="free-models.yaml")
    p.add_argument("--out", default="rendered/config.yaml")
    a = p.parse_args(argv)
    cfg = yaml.safe_load(pathlib.Path(a.config).read_text())
    free = yaml.safe_load(pathlib.Path(a.free).read_text())
    errors = check(cfg, free)
    if errors:
        stale = pathlib.Path(a.out)
        if stale.exists():
            stale.unlink()  # a reboot must not serve the last rendered config either
        print("free guard: refused, nothing rendered, the last rendered config removed:")
        for e in errors:
            print(f"  - {e}")
        return 3  # a free-only refusal; the Makefile stops LiteLLM only on this code
    rendered, left = render(cfg, os.environ)
    for x in left:
        print(f"free guard: left out {x['id']} (no {', '.join(x['missing'])})")
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# Rendered by scripts/free_guard.py from config.yaml. Do not edit.\n"
                   + yaml.safe_dump(rendered, sort_keys=False))
    print(f"free guard: ok, {len(rendered['model_list'])} deployments rendered to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
