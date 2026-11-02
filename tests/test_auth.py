"""The auth hook: unit cell CFG-23 and the hook's decisions. Runs in the LiteLLM image."""
import asyncio
import importlib.util
import os
import pathlib
import unittest
from unittest import mock

from fastapi import HTTPException

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOOD = "sk-" + "a" * 48
ADMIN = "sk-" + "m" * 48
METRICS = "sk-" + "t" * 48
ENV = {"CHARMER_API_KEY": GOOD, "LITELLM_MASTER_KEY": ADMIN, "CHARMER_METRICS_KEY": METRICS}


def load_auth(env):
    with mock.patch.dict(os.environ, env, clear=False):
        for k in ("CHARMER_API_KEY", "LITELLM_MASTER_KEY", "CHARMER_METRICS_KEY"):
            if k not in env:
                os.environ.pop(k, None)
        spec = importlib.util.spec_from_file_location(f"charmer_auth_{id(env)}", ROOT / "plugin" / "charmer_auth.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def request(path, headers=None):
    req = mock.Mock()
    req.url.path = path
    req.headers = headers or {}
    return req


def call(mod, path, key, headers=None):
    return asyncio.run(mod.user_api_key_auth(request(path, headers), key))




class Start(unittest.TestCase):
    def test_cfg23_bad_keys_refused_at_start(self):
        """CFG-23: CHARMER_API_KEY missing, shorter than 32 characters, or equal to the admin or metrics key stops the
        start; so does a LITELLM_MASTER_KEY under 48 characters (LiteLLM would run in no-auth dev mode)."""
        bad = [
            {"LITELLM_MASTER_KEY": ADMIN},
            {"CHARMER_API_KEY": "sk-short", "LITELLM_MASTER_KEY": ADMIN},
            {"CHARMER_API_KEY": ADMIN, "LITELLM_MASTER_KEY": ADMIN},
            {"CHARMER_API_KEY": METRICS, "LITELLM_MASTER_KEY": ADMIN, "CHARMER_METRICS_KEY": METRICS},
            {"CHARMER_API_KEY": GOOD},
            {"CHARMER_API_KEY": GOOD, "LITELLM_MASTER_KEY": "sk-short"},
        ]
        for env in bad:
            with self.subTest(env=sorted(env)):
                with self.assertRaises(SystemExit):
                    load_auth(env)

    def test_no_keys_file_is_read(self):
        """One key for one user: the hook needs no keys.yaml."""
        self.assertNotIn("keys.yaml", (ROOT / "plugin" / "charmer_auth.py").read_text())


class Hook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = load_auth(ENV)

    def test_api_key_may_call_every_group(self):
        u = call(self.auth, "/v1/chat/completions", GOOD)
        self.assertEqual(u.key_alias, "app")
        self.assertEqual(list(u.models or []), [], "an empty list lets the key call every group; the plugin refuses unknown ones")
        self.assertNotEqual(u.api_key, GOOD, "the raw key must never be stored on the auth object")

    def test_unknown_key_is_401_with_the_recorded_message(self):
        with self.assertRaises(Exception) as e:
            call(self.auth, "/v1/chat/completions", "sk-" + "z" * 48)
        self.assertNotIsInstance(e.exception, HTTPException)
        self.assertEqual(str(e.exception), "charmer: unknown key")

    def test_consumer_key_only_reaches_inference_paths(self):
        for path in ("/v1/chat/completions", "/chat/completions", "/v1/embeddings", "/embeddings", "/v1/models", "/models"):
            call(self.auth, path, GOOD)
        for path in ("/health", "/model/info", "/metrics", "/key/generate", "/v1/files", "/v1/responses", "/v1/messages", "/routes"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPException) as e:
                    call(self.auth, path, GOOD)
                self.assertEqual(e.exception.status_code, 403)

    def test_admin_key_is_proxy_admin_on_the_host_only(self):
        u = call(self.auth, "/model/info", ADMIN)
        self.assertEqual(u.user_role.value, "proxy_admin")
        with self.assertRaises(Exception):
            call(self.auth, "/model/info", ADMIN, {"cf-connecting-ip": "1.2.3.4"})

    def test_metrics_key_reaches_metrics_only(self):
        call(self.auth, "/metrics", METRICS)
        call(self.auth, "/metrics/", METRICS)
        with self.assertRaises(HTTPException):
            call(self.auth, "/v1/models", METRICS)
        with self.assertRaises(Exception):
            call(self.auth, "/metrics", METRICS, {"cf-connecting-ip": "1.2.3.4"})


if __name__ == "__main__":
    unittest.main()
