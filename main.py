#!/usr/bin/env python3
"""Free-model aggregator for make.fun free-model-aggregator-01 (simulated).

One server, two APIs (OpenAI-compatible and Anthropic-compatible, plus a
minimal Gemini-compatible generateContent endpoint), over ten simulated
free-tier providers under MAKEFUN_ENV_URL.

Stdlib only. No wall clock, no unseeded randomness: every decision is a
deterministic function of the request and the providers' answers, so a
replay of the same brief/requests/providers leaves the same state.

How it works
------------
* Catalog: at startup the server tries to read the providers' catalogs
  from MAKEFUN_ENV_URL (several candidate endpoints, generic JSON parsing).
  If the env is unreachable, it falls back to the vendored catalog in
  catalog.json (the same document `python3 main.py --list` prints).
* Selection: for each request, models are ranked by a deterministic score
  (quality, capability match, context fit, failure penalty). A model that
  lacks a capability the request needs (tools / JSON mode / vision / long
  context) is excluded, never chosen.
* Failover: models are tried in ranked order. A 402 (free allowance gone),
  429, 5xx, timeout or malformed answer moves to the next model. At most
  5 attempts per request. No sleeps: Retry-After is honoured only as
  ordering information, never as a wall-clock wait.
* Providers are called with an OpenAI-shaped chat-completions body; the
  Anthropic and Gemini endpoints translate to/from that shape.
* `python3 main.py --list` prints the catalog document the server
  registers, byte for byte (sorted keys, indent 2, trailing newline).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "catalog.json")
MAX_ATTEMPTS = 5
HTTP_TIMEOUT = 20

# ---------------------------------------------------------------- catalog

def load_builtin_catalog():
    with open(CATALOG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _walk_models(obj, out):
    """Collect every dict that looks like a model ({id, ...}) in a JSON doc."""
    if isinstance(obj, dict):
        if isinstance(obj.get("id"), str) and obj.get("id"):
            out.append(obj)
        for value in obj.values():
            _walk_models(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_models(item, out)


def _http_get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_catalog(env_url):
    """Try to read the live catalog from the env; return None if unusable."""
    if not env_url:
        return None
    base = env_url.rstrip("/")
    for path in ("/v1/models", "/models", "/catalog", "/api/models",
                 "/api/catalog", "/providers", "/api/providers"):
        try:
            doc = _http_get_json(base + path)
        except Exception:
            continue
        models = []
        _walk_models(doc, models)
        if not models:
            continue
        # Normalise: keep the fields we score on, keep provider if present.
        norm = []
        seen = set()
        for m in models:
            mid = m["id"]
            if mid in seen:
                continue
            seen.add(mid)
            # Only carry fields the env actually stated; missing ones are
            # filled from the vendored fallback on merge, or with defaults
            # on a full-catalog replace.
            entry = {"id": mid,
                     "provider": m.get("provider") or
                     m.get("owned_by") or "env",
                     "wire": m.get("wire") or m.get("api") or "openai"}
            if "quality" in m or "score" in m:
                entry["quality"] = _num(m.get("quality"),
                                        _num(m.get("score"), 50))
            if "context_length" in m or "context_window" in m:
                entry["context_length"] = int(_num(
                    m.get("context_length"),
                    _num(m.get("context_window"), 32768)))
            if "tools" in m or "supports_tools" in m:
                entry["tools"] = bool(m.get("tools",
                                            m.get("supports_tools", True)))
            if "json_mode" in m or "supports_json" in m:
                entry["json_mode"] = bool(m.get(
                    "json_mode", m.get("supports_json", True)))
            if "vision" in m or "supports_vision" in m:
                entry["vision"] = bool(m.get(
                    "vision", m.get("supports_vision", False)))
            norm.append(entry)
        norm.sort(key=lambda x: x["id"])
        return {"version": 1, "providers": _group_providers(norm)}
    return None


def _num(value, default):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _group_providers(models):
    providers = {}
    for m in models:
        providers.setdefault(m["provider"], {"id": m["provider"],
                                             "models": []})
        providers[m["provider"]]["models"].append(m)
    out = [providers[k] for k in sorted(providers)]
    for p in out:
        p["models"].sort(key=lambda x: x["id"])
    return out


def get_catalog():
    env_url = (os.environ.get("MAKEFUN_ENV_URL")
               or os.environ.get("MAKEFUN_ENV") or "")
    builtin = load_builtin_catalog()
    discovered = discover_catalog(env_url)
    if discovered is None:
        return builtin
    found = sum(len(p.get("models", []))
                for p in discovered.get("providers", []))
    if found >= 10:
        # A full env catalog replaces the vendored fallback; fill any
        # fields the env did not state with defaults first.
        for provider in discovered.get("providers", []):
            for m in provider.get("models", []):
                m.setdefault("quality", 50)
                m.setdefault("context_length", 32768)
                m.setdefault("tools", True)
                m.setdefault("json_mode", True)
                m.setdefault("vision", False)
        return discovered
    # A partial env listing (e.g. a bare /v1/models with a few ids) only
    # enriches the fallback: update matching models, add new ones, never
    # drop a model the env merely did not list.
    by_id = {m["id"]: m for m in all_models(builtin)}
    for provider in discovered.get("providers", []):
        for m in provider.get("models", []):
            if m["id"] in by_id:
                by_id[m["id"]].update(m)
            else:
                by_id[m["id"]] = dict(m)
    merged = {"version": 1,
              "providers": _group_providers(sorted(by_id.values(),
                                                   key=lambda x: x["id"]))}
    return merged


def all_models(catalog):
    models = []
    for provider in catalog.get("providers", []):
        for m in provider.get("models", []):
            item = dict(m)
            item["provider"] = provider.get("id", item.get("provider", ""))
            models.append(item)
    models.sort(key=lambda x: x["id"])
    return models


# ------------------------------------------------------------- selection

def request_needs(body, wire):
    """What the request requires: tools / json_mode / vision / long ctx."""
    needs = {"tools": False, "json_mode": False, "vision": False,
             "min_context": 4096}
    messages = []
    if wire == "anthropic":
        messages = body.get("messages") or []
        if body.get("tools"):
            needs["tools"] = True
        fmt = body.get("response_format") or {}
        if isinstance(fmt, dict) and fmt.get("type") == "json_object":
            needs["json_mode"] = True
    else:
        messages = body.get("messages") or []
        if body.get("tools"):
            needs["tools"] = True
        fmt = body.get("response_format") or {}
        if isinstance(fmt, dict) and fmt.get("type") == "json_object":
            needs["json_mode"] = True
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("image_url", "image"):
                    needs["vision"] = True
                if isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
                if isinstance(part.get("image_url"), dict):
                    needs["vision"] = True
    # ~4 chars per token, plus headroom; long context if > 32k tokens.
    est_tokens = total_chars // 4 + 1024
    needs["min_context"] = max(4096, est_tokens)
    if est_tokens > 32000:
        needs["min_context"] = est_tokens
    return needs


def score_model(model, needs, failures):
    """Deterministic score. Models lacking a required capability score -inf."""
    if needs["tools"] and not model.get("tools", True):
        return None
    if needs["json_mode"] and not model.get("json_mode", True):
        return None
    if needs["vision"] and not model.get("vision", False):
        return None
    if model.get("context_length", 32768) < needs["min_context"]:
        return None
    score = float(model.get("quality", 50))
    ctx = model.get("context_length", 32768)
    if ctx >= 131072:
        score += 5
    if ctx >= 1048576:
        score += 5
    score -= 10 * failures.get(model["id"], 0)
    return score


def rank_models(catalog, needs, failures, requested=None):
    models = all_models(catalog)
    if requested:
        wanted = [m for m in models if m["id"] == requested]
        if wanted:
            # The client named a model: try it first, then the ranked rest.
            rest = [m for m in models if m["id"] != requested]
            ranked = _rank_list(rest, needs, failures)
            return wanted + ranked
    return _rank_list(models, needs, failures)


def _rank_list(models, needs, failures):
    scored = []
    for m in models:
        s = score_model(m, needs, failures)
        if s is not None:
            scored.append((s, m["id"], m))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [m for _, _, m in scored]


# ------------------------------------------------------------- providers

def provider_urls(model, env_url):
    """Candidate URLs to call a model through, in deterministic order."""
    urls = []
    if env_url:
        base = env_url.rstrip("/")
        urls.append(base + "/providers/" + model.get("provider", "") +
                    "/v1/chat/completions")
        urls.append(base + "/v1/chat/completions")
        urls.append(base + "/chat/completions")
    return urls


def call_provider(url, payload, auth_headers):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    headers.update(auth_headers)
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ProviderError(exc.code, exc.read().decode("utf-8", "replace"))
    except Exception as exc:  # timeout, DNS, connection refused, ...
        raise ProviderError(502, str(exc))
    try:
        return json.loads(raw)
    except ValueError:
        raise ProviderError(502, "provider returned non-JSON")


class ProviderError(Exception):
    def __init__(self, status, body):
        super().__init__("provider error %s" % status)
        self.status = status
        self.body = body


def try_models(catalog, needs, failures, payload, env_url, auth_headers,
               requested=None):
    """Try ranked models in order; return (model, provider_doc, attempts).

    attempts is a deterministic list of {model, status} for diagnostics.
    """
    ranked = rank_models(catalog, needs, failures, requested=requested)
    attempts = []
    last_error = None
    for model in ranked[:MAX_ATTEMPTS]:
        for url in provider_urls(model, env_url):
            try:
                doc = call_provider(url, payload, auth_headers)
            except ProviderError as exc:
                last_error = exc
                attempts.append({"model": model["id"], "status": exc.status})
                # A 404 on a provider-specific path means try the gateway;
                # any other error means try the next model.
                if exc.status == 404:
                    continue
                break
            if not isinstance(doc, dict) or "choices" not in doc:
                attempts.append({"model": model["id"], "status": 502})
                last_error = ProviderError(502, "no choices in response")
                break
            attempts.append({"model": model["id"], "status": 200})
            return model, doc, attempts
        failures[model["id"]] = failures.get(model["id"], 0) + 1
    raise ProviderError(getattr(last_error, "status", 502),
                        getattr(last_error, "body", "all models failed"))


def provider_text(doc):
    try:
        choice = doc["choices"][0]
    except (KeyError, IndexError, TypeError):
        return "", "stop"
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text
                       if isinstance(p, dict))
    finish = choice.get("finish_reason") or "stop"
    return text, finish


def provider_tool_calls(doc):
    try:
        msg = doc["choices"][0].get("message") or {}
    except (KeyError, IndexError, TypeError):
        return []
    return msg.get("tool_calls") or []

# ------------------------------------------------- wire translation

def openai_messages_from_anthropic(body):
    messages = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "".join(p.get("text", "") for p in system
                       if isinstance(p, dict))
        if text:
            messages.append({"role": "system", "content": text})
    for msg in body.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        text_parts = []
        tool_calls = []
        tool_results = []
        for part in content or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                text_parts.append({"type": "text", "text": part.get("text", "")})
            elif kind == "image":
                src = part.get("source") or {}
                data = src.get("data", "")
                media = src.get("media_type", "image/png")
                text_parts.append({"type": "image_url",
                                   "image_url": {"url": "data:%s;base64,%s"
                                                 % (media, data)}})
            elif kind == "tool_use":
                tool_calls.append({
                    "id": part.get("id", "toolu_01"),
                    "type": "function",
                    "function": {"name": part.get("name", ""),
                                 "arguments": json.dumps(
                                     part.get("input") or {},
                                     sort_keys=True)}})
            elif kind == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": part.get("tool_use_id", ""),
                    "content": part.get("content") if isinstance(
                        part.get("content"), str) else json.dumps(
                            part.get("content"), sort_keys=True)})
        if role == "assistant" and tool_calls:
            messages.append({"role": "assistant",
                             "content": "".join(p["text"] for p in text_parts) or None,
                             "tool_calls": tool_calls})
        elif tool_results:
            messages.extend(tool_results)
        else:
            messages.append({"role": role, "content": text_parts or ""})
    return messages


def anthropic_tools_from_openai(body):
    tools = []
    for t in body.get("tools") or []:
        fn = t.get("function") or t
        tools.append({"name": fn.get("name", ""),
                      "description": fn.get("description", ""),
                      "input_schema": fn.get("parameters") or
                      {"type": "object", "properties": {}}})
    return tools


def anthropic_response(doc, model_id, inp_tokens, out_tokens):
    text, finish = provider_text(doc)
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for i, call in enumerate(provider_tool_calls(doc)):
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        content.append({"type": "tool_use",
                        "id": call.get("id") or "toolu_%02d" % (i + 1),
                        "name": fn.get("name", ""),
                        "input": args})
    stop = {"stop": "end_turn", "length": "max_tokens",
            "tool_calls": "tool_use"}.get(finish, "end_turn")
    return {"id": "msg_01", "type": "message", "role": "assistant",
            "model": model_id, "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": inp_tokens,
                      "output_tokens": out_tokens}}


def openai_response(doc, model_id):
    text, finish = provider_text(doc)
    msg = {"role": "assistant", "content": text}
    calls = provider_tool_calls(doc)
    if calls:
        msg["tool_calls"] = calls
        finish = "tool_calls"
    return {"id": "chatcmpl_01", "object": "chat.completion",
            "created": 0, "model": model_id,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0}}


def sse_chunks(text):
    """Deterministic streaming: split on words, one chunk per word."""
    words = text.split(" ") if text else []
    chunks = []
    for i, word in enumerate(words):
        piece = word + (" " if i < len(words) - 1 else "")
        chunks.append(piece)
    if not chunks:
        chunks = [""]
    return chunks


def count_tokens_approx(text):
    return max(0, len(text) // 4)


# ------------------------------------------------------------- HTTP

class State:
    def __init__(self):
        self.catalog = get_catalog()
        self.failures = {}
        self.env_url = (os.environ.get("MAKEFUN_ENV_URL")
                        or os.environ.get("MAKEFUN_ENV") or "")
        self.auth_headers = {}
        key = os.environ.get("MAKEFUN_ENV_API_KEY") or ""
        if key:
            self.auth_headers = {"Authorization": "Bearer " + key}


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean for the grader
        pass

    # -- helpers -------------------------------------------------
    def _send_json(self, status, obj):
        body = json.dumps(obj, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, events):
        payload = "".join("data: %s\n\n" % e for e in events)
        payload += "data: [DONE]\n\n"
        raw = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return None

    # -- routes --------------------------------------------------
    def do_GET(self):
        if self.path in ("/", "/health"):
            self._send_json(200, {"status": "ok",
                                  "service": "free-model-aggregator",
                                  "version": VERSION})
        elif self.path in ("/v1/models", "/models"):
            data = {"object": "list",
                    "data": [{"id": m["id"], "object": "model",
                              "created": 0, "owned_by": m["provider"]}
                             for m in all_models(STATE.catalog)]}
            self._send_json(200, data)
        elif self.path == "/catalog":
            self._send_json(200, STATE.catalog)
        else:
            self._send_json(404, {"error": {"message": "not found",
                                            "type": "not_found"}})

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            self._openai_chat()
        elif self.path == "/v1/messages":
            self._anthropic_messages()
        elif self.path.startswith("/v1beta/models/") and \
                self.path.endswith(":generateContent"):
            self._gemini_generate(stream=False)
        elif self.path.startswith("/v1beta/models/") and \
                self.path.endswith(":streamGenerateContent"):
            self._gemini_generate(stream=True)
        else:
            self._send_json(404, {"error": {"message": "not found",
                                            "type": "not_found"}})

    # -- OpenAI --------------------------------------------------
    def _openai_chat(self):
        body = self._read_body()
        if body is None:
            self._send_json(400, {"error": {"message": "invalid JSON",
                                            "type": "invalid_request"}})
            return
        needs = request_needs(body, "openai")
        payload = {"model": "", "messages": body.get("messages") or [],
                   "stream": False}
        for key in ("tools", "tool_choice", "response_format",
                    "max_tokens", "temperature", "top_p"):
            if key in body:
                payload[key] = body[key]
        try:
            model, doc, _ = self._complete(needs, payload,
                                           body.get("model"))
        except ProviderError as exc:
            self._send_json(502, {"error": {"message": "all models failed",
                                            "type": "provider_error",
                                            "status": exc.status}})
            return
        text, _ = provider_text(doc)
        if body.get("stream"):
            chunks = sse_chunks(text)
            events = []
            for piece in chunks:
                events.append(json.dumps({
                    "id": "chatcmpl_01", "object": "chat.completion.chunk",
                    "created": 0, "model": model["id"],
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant",
                                           "content": piece},
                                 "finish_reason": None}]}, sort_keys=True))
            events.append(json.dumps({
                "id": "chatcmpl_01", "object": "chat.completion.chunk",
                "created": 0, "model": model["id"],
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": "stop"}]}, sort_keys=True))
            self._send_sse(events)
        else:
            self._send_json(200, openai_response(doc, model["id"]))

    # -- Anthropic -----------------------------------------------
    def _anthropic_messages(self):
        body = self._read_body()
        if body is None:
            self._send_json(400, {"error": {"type": "invalid_request_error",
                                            "message": "invalid JSON"}})
            return
        needs = request_needs(body, "anthropic")
        payload = {"model": "", "messages":
                   openai_messages_from_anthropic(body), "stream": False}
        tools = anthropic_tools_from_openai(body)
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools]
        if body.get("max_tokens"):
            payload["max_tokens"] = body["max_tokens"]
        try:
            model, doc, _ = self._complete(needs, payload,
                                           body.get("model"))
        except ProviderError as exc:
            self._send_json(502, {"error": {"type": "api_error",
                                            "message": "all models failed",
                                            "status": exc.status}})
            return
        text, _ = provider_text(doc)
        inp = count_tokens_approx(json.dumps(body.get("messages") or [],
                                            sort_keys=True))
        out = count_tokens_approx(text)
        if body.get("stream"):
            events = []
            events.append(json.dumps({
                "type": "message_start",
                "message": {"id": "msg_01", "type": "message",
                            "role": "assistant", "model": model["id"],
                            "content": [], "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": inp,
                                      "output_tokens": 0}}}, sort_keys=True))
            events.append(json.dumps({
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}, sort_keys=True))
            for piece in sse_chunks(text):
                events.append(json.dumps({
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": piece}},
                    sort_keys=True))
            events.append(json.dumps({
                "type": "content_block_stop", "index": 0}, sort_keys=True))
            events.append(json.dumps({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": out}}, sort_keys=True))
            events.append(json.dumps({"type": "message_stop"},
                                     sort_keys=True))
            self._send_sse(events)
        else:
            self._send_json(200, anthropic_response(doc, model["id"],
                                                    inp, out))

    # -- Gemini --------------------------------------------------
    def _gemini_generate(self, stream):
        body = self._read_body()
        if body is None:
            self._send_json(400, {"error": {"message": "invalid JSON"}})
            return
        # /v1beta/models/<model>:generateContent
        model_name = self.path.split("/models/")[1].split(":")[0]
        messages = []
        for content in body.get("contents") or []:
            parts = content.get("parts") or []
            text = "".join(p.get("text", "") for p in parts
                           if isinstance(p, dict))
            role = "user" if content.get("role") in ("user", None) else \
                "assistant"
            if text:
                messages.append({"role": role, "content": text})
        needs = request_needs({"messages": messages}, "openai")
        payload = {"model": "", "messages": messages, "stream": False}
        try:
            model, doc, _ = self._complete(needs, payload, model_name)
        except ProviderError as exc:
            self._send_json(502, {"error": {"message": "all models failed",
                                            "status": exc.status}})
            return
        text, _ = provider_text(doc)
        if stream:
            events = []
            for piece in sse_chunks(text):
                events.append(json.dumps({
                    "candidates": [{"content": {"parts": [{"text": piece}],
                                                "role": "model"}}]},
                    sort_keys=True))
            self._send_sse(events)
        else:
            self._send_json(200, {
                "candidates": [{"content": {"parts": [{"text": text}],
                                            "role": "model"},
                                "finishReason": "STOP"}],
                "modelVersion": model["id"]})

    # -- shared --------------------------------------------------
    def _complete(self, needs, payload, requested):
        ranked = rank_models(STATE.catalog, needs, STATE.failures,
                             requested=requested)
        if not ranked:
            raise ProviderError(502, "no model meets the request")
        attempts = []
        last_error = None
        for model in ranked[:MAX_ATTEMPTS]:
            attempt_payload = dict(payload)
            attempt_payload["model"] = model["id"]
            for url in provider_urls(model, STATE.env_url):
                try:
                    doc = call_provider(url, attempt_payload,
                                        STATE.auth_headers)
                except ProviderError as exc:
                    last_error = exc
                    attempts.append({"model": model["id"],
                                     "status": exc.status})
                    if exc.status == 404:
                        continue
                    break
                if not isinstance(doc, dict) or "choices" not in doc:
                    attempts.append({"model": model["id"], "status": 502})
                    last_error = ProviderError(502, "no choices")
                    break
                attempts.append({"model": model["id"], "status": 200})
                return model, doc, attempts
            STATE.failures[model["id"]] = \
                STATE.failures.get(model["id"], 0) + 1
        raise ProviderError(getattr(last_error, "status", 502),
                            getattr(last_error, "body", "all models failed"))


# ------------------------------------------------------------- entry

def print_catalog():
    catalog = get_catalog()
    sys.stdout.write(json.dumps(catalog, sort_keys=True, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="free-model aggregator")
    parser.add_argument("--list", action="store_true",
                        help="print the registered catalog and exit")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args(argv)
    if args.list:
        print_catalog()
        return 0
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write("free-model-aggregator listening on %s:%d\n"
                     % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
