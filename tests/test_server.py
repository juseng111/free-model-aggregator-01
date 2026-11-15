"""Tests for main.py against a simulated provider env (mock).

The mock env mimics the bounty description: ten simulated providers under
MAKEFUN_ENV_URL, one of them (fjord-free-opus, the highest-quality model)
always answers 402 (free allowance gone), so the server must fail over to
the next best model. No network beyond localhost, stdlib only.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MOCK_PORT = 18711
APP_PORT = 18712
ENV_URL = "http://127.0.0.1:%d" % MOCK_PORT
APP_URL = "http://127.0.0.1:%d" % APP_PORT


class MockEnv(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, obj):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "atlas-free-large", "owned_by": "atlas"},
                {"id": "fjord-free-opus", "owned_by": "fjord"},
            ]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model", "")
        if model == "fjord-free-opus":
            # Free allowance gone on the best model: must fail over.
            self._json(402, {"error": {"message": "free allowance gone",
                                       "type": "insufficient_quota"}})
            return
        if model == "atlas-free-small":
            self._json(429, {"error": {"message": "rate limited",
                                       "type": "rate_limit"}})
            return
        text = "mock answer from %s" % model
        self._json(200, {
            "id": "chatcmpl_mock", "object": "chat.completion",
            "created": 0, "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15}})


def http_json(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockEnv)
        threading.Thread(target=cls.mock.serve_forever,
                         daemon=True).start()
        env = dict(os.environ, MAKEFUN_ENV_URL=ENV_URL,
                   PORT=str(APP_PORT))
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "main.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for the app to answer /health (bounded, deterministic).
        for _ in range(50):
            try:
                status, _ = http_json("GET", APP_URL + "/health")
                if status == 200:
                    break
            except Exception:
                pass
            import time
            time.sleep(0.1)
        else:
            raise RuntimeError("app did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.mock.shutdown()

    def test_list_is_deterministic(self):
        out1 = subprocess.run([sys.executable,
                               os.path.join(ROOT, "main.py"), "--list"],
                              capture_output=True, text=True, check=True,
                              env=dict(os.environ,
                                       MAKEFUN_ENV_URL="")).stdout
        out2 = subprocess.run([sys.executable,
                               os.path.join(ROOT, "main.py"), "--list"],
                              capture_output=True, text=True, check=True,
                              env=dict(os.environ,
                                       MAKEFUN_ENV_URL="")).stdout
        self.assertEqual(out1, out2)
        catalog = json.loads(out1)
        self.assertEqual(len(catalog["providers"]), 10)
        self.assertEqual(sum(len(p["models"])
                             for p in catalog["providers"]), 20)

    def test_models_endpoint(self):
        status, raw = http_json("GET", APP_URL + "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)["data"]), 20)

    def test_openai_chat_fails_over_from_402(self):
        # Top model fjord-free-opus is 402 in the mock; expect atlas-large.
        status, raw = http_json("POST", APP_URL + "/v1/chat/completions",
                                {"messages": [{"role": "user",
                                               "content": "Hello"}]})
        self.assertEqual(status, 200)
        doc = json.loads(raw)
        self.assertEqual(doc["model"], "atlas-free-large")
        self.assertEqual(doc["choices"][0]["message"]["content"],
                         "mock answer from atlas-free-large")

    def test_openai_chat_deterministic_repeat(self):
        _, r1 = http_json("POST", APP_URL + "/v1/chat/completions",
                          {"messages": [{"role": "user", "content": "Hi"}]})
        _, r2 = http_json("POST", APP_URL + "/v1/chat/completions",
                          {"messages": [{"role": "user", "content": "Hi"}]})
        self.assertEqual(json.loads(r1)["model"], json.loads(r2)["model"])

    def test_openai_chat_tools_and_json_mode(self):
        status, raw = http_json("POST", APP_URL + "/v1/chat/completions",
                                {"messages": [{"role": "user",
                                               "content": "What is 2+2?"}],
                                 "tools": [{"type": "function",
                                            "function": {
                                                "name": "calc",
                                                "parameters": {
                                                    "type": "object",
                                                    "properties": {}}}}],
                                 "response_format": {"type": "json_object"}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["model"], "atlas-free-large")

    def test_openai_chat_streaming(self):
        status, raw = http_json("POST", APP_URL + "/v1/chat/completions",
                                {"messages": [{"role": "user",
                                               "content": "Hello"}],
                                 "stream": True})
        self.assertEqual(status, 200)
        self.assertIn("data: [DONE]", raw)
        text = ""
        for line in raw.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line[6:])
                delta = chunk["choices"][0]["delta"]
                text += delta.get("content", "")
        self.assertEqual(text, "mock answer from atlas-free-large")

    def test_openai_chat_vision_prefers_vision_model(self):
        status, raw = http_json("POST", APP_URL + "/v1/chat/completions",
                                {"messages": [{"role": "user", "content": [
                                    {"type": "text", "text": "see this"},
                                    {"type": "image_url",
                                     "image_url": {"url": "data:image/png;base64,AAA"}}]}]})
        self.assertEqual(status, 200)
        # fjord-opus (vision, 402) -> atlas-large (vision) must win.
        self.assertEqual(json.loads(raw)["model"], "atlas-free-large")

    def test_anthropic_messages(self):
        status, raw = http_json("POST", APP_URL + "/v1/messages",
                                {"model": "beacon-free-sonnet",
                                 "max_tokens": 100,
                                 "messages": [{"role": "user",
                                               "content": "Hello"}]})
        self.assertEqual(status, 200)
        doc = json.loads(raw)
        self.assertEqual(doc["type"], "message")
        self.assertEqual(doc["content"][0]["text"],
                         "mock answer from beacon-free-sonnet")

    def test_anthropic_streaming(self):
        status, raw = http_json("POST", APP_URL + "/v1/messages",
                                {"model": "beacon-free-sonnet",
                                 "max_tokens": 100, "stream": True,
                                 "messages": [{"role": "user",
                                               "content": "Hello"}]})
        self.assertEqual(status, 200)
        self.assertIn("content_block_delta", raw)
        self.assertIn("message_stop", raw)

    def test_gemini_generate(self):
        status, raw = http_json(
            "POST",
            APP_URL + "/v1beta/models/drift-free-flash:generateContent",
            {"contents": [{"parts": [{"text": "Hello"}]}]})
        self.assertEqual(status, 200)
        doc = json.loads(raw)
        self.assertEqual(doc["candidates"][0]["content"]["parts"][0]["text"],
                         "mock answer from drift-free-flash")

    def test_long_context_excludes_small_models(self):
        long_text = "x" * 140000
        status, raw = http_json("POST", APP_URL + "/v1/chat/completions",
                                {"messages": [{"role": "user",
                                               "content": long_text}]})
        self.assertEqual(status, 200)
        model = json.loads(raw)["model"]
        # Small-context models (<=32768) must never be picked here.
        self.assertNotIn(model, ("atlas-free-small", "cinder-free-chat",
                                 "drift-free-lite", "fjord-free-tiny",
                                 "harbor-free-nano", "inlet-free-fast",
                                 "juniper-free-echo"))


if __name__ == "__main__":
    unittest.main()
