"""Behavioural tests for the chat engine.

Runs without a network connection or real credentials: the Anthropic client
and the Supabase client are both replaced with stubs. Run it directly:

    pip install -r requirements-dev.txt
    python3 test_main.py

Exits non-zero if anything fails, so it can be wired into CI as-is.
"""
import importlib
import os
import sys
import types

import httpx

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

import anthropic  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VALID_KEY = "sk-ant-api03-" + "y" * 60

results = []


def check(name, got, want):
    results.append((name, got, want))


def load(api_key=VALID_KEY, auth_token=None, workspace_id=None):
    """Import main.py fresh with a given credential environment."""
    sys.modules.pop("main", None)
    for var, value in (("ANTHROPIC_API_KEY", api_key), ("ANTHROPIC_AUTH_TOKEN", auth_token),
                       ("ANTHROPIC_WORKSPACE_ID", workspace_id)):
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    return importlib.import_module("main")


# --- stubs ---------------------------------------------------------------
def text_block(t):
    return types.SimpleNamespace(type="text", text=t)


def thinking_block():
    # A real ThinkingBlock genuinely has no .text attribute.
    return types.SimpleNamespace(type="thinking", thinking="...", signature="x")


def reply(blocks, stop_reason="end_turn"):
    return types.SimpleNamespace(content=blocks, stop_reason=stop_reason)


def api_error(cls, status):
    def raiser(**kwargs):
        raise cls(
            message="boom",
            response=httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com")),
            body=None,
        )
    return raiser


class FakeSupabase:
    """Minimal stand-in for the chained supabase query builder."""

    def __init__(self, row):
        self.row = row
        self.updated = None

    def table(self, _name):
        return self

    def select(self, _cols):
        self._op = ("select", None)
        return self

    def update(self, payload):
        self._op = ("update", payload)
        return self

    def eq(self, _col, _val):
        return self

    def execute(self):
        op, payload = self._op
        if op == "update":
            self.updated = payload
            return types.SimpleNamespace(data=[payload])
        return types.SimpleNamespace(data=[self.row] if self.row else [])


def capture(main, response):
    """Record the kwargs main.py passes to the Anthropic client."""
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return response

    main.anthropic_client.messages.create = fake_create
    return seen


# --- credential handling -------------------------------------------------
main = load(api_key=None)
check("no credential detected", main.has_anthropic_credential, False)
client = TestClient(main.app, raise_server_exceptions=False)
res = client.post("/api/chat", json={"message": "hi"})
check("no credential -> 503", res.status_code, 503)
check("no credential leaks nothing", "401" not in res.json()["detail"], True)
health = client.get("/api/health").json()
check("health degraded", health["status"], "degraded")
check("health hides secret", "sk-ant" not in str(health), True)

main = load(api_key='"' + VALID_KEY + '"\n')
check("quoted/newline key sanitized", main.anthropic_api_key, VALID_KEY)
check("sanitized key is clean", main.anthropic_key_problem, None)

main = load(api_key="   ")
check("blank key flagged", bool(main.anthropic_key_problem), True)
check("blank key -> 503", TestClient(main.app, raise_server_exceptions=False)
      .post("/api/chat", json={"message": "hi"}).status_code, 503)

main = load(api_key=None, auth_token="sk-ant-oat01-" + "z" * 50)
check("oauth token accepted", main.has_anthropic_credential, True)

# --- error mapping -------------------------------------------------------
main = load()
client = TestClient(main.app, raise_server_exceptions=False)

main.anthropic_client.messages.create = api_error(anthropic.AuthenticationError, 401)
res = client.post("/api/chat", json={"message": "hi"})
check("401 -> 503", res.status_code, 503)
check("401 body has no provider text", "401" not in res.json()["detail"], True)

main.anthropic_client.messages.create = api_error(anthropic.RateLimitError, 429)
check("rate limit -> 429", client.post("/api/chat", json={"message": "hi"}).status_code, 429)

main.anthropic_client.messages.create = api_error(anthropic.InternalServerError, 500)
res = client.post("/api/chat", json={"message": "hi"})
check("upstream 5xx -> 502", res.status_code, 502)
check("5xx invites a retry", "try again" in res.json()["detail"], True)

# A malformed request (unservable model, rejected param, or an unscoped key
# sent without a workspace id) fails the same way on every retry.
main.anthropic_client.messages.create = api_error(anthropic.BadRequestError, 400)
res = client.post("/api/chat", json={"message": "hi"})
check("400 -> 502", res.status_code, 502)
check("400 does NOT invite a retry", "retrying will not help" in res.json()["detail"], True)

main.anthropic_client.messages.create = api_error(anthropic.NotFoundError, 404)
res = client.post("/api/chat", json={"message": "hi"})
check("unknown model -> 502", res.status_code, 502)
check("unknown model does NOT invite a retry", "retrying will not help" in res.json()["detail"], True)

# --- workspace scoping ---------------------------------------------------
def workspace_header(m):
    return {k.lower(): v for k, v in m.anthropic_client.default_headers.items()}.get("anthropic-workspace-id")

scoped = load(workspace_id="wrkspc_abc123")
check("workspace header sent when set", workspace_header(scoped), "wrkspc_abc123")
check("workspace shown on health", TestClient(scoped.app).get("/api/health").json()["anthropic_workspace_id_set"], True)

unscoped = load(workspace_id=None)
check("no workspace header when unset", workspace_header(unscoped), None)
check("workspace absent on health", TestClient(unscoped.app).get("/api/health").json()["anthropic_workspace_id_set"], False)

quoted = load(workspace_id='"wrkspc_xyz"\n')
check("workspace id sanitized", workspace_header(quoted), "wrkspc_xyz")

# --- reply extraction ----------------------------------------------------
capture(main, reply([thinking_block(), text_block("Hello.")]))
res = client.post("/api/chat", json={"message": "hi"})
check("thinking block first -> 200", res.status_code, 200)
check("thinking block first -> text", res.json()["reply"], "Hello.")

capture(main, reply([], stop_reason="refusal"))
check("refusal -> 422", client.post("/api/chat", json={"message": "hi"}).status_code, 422)

capture(main, reply([thinking_block()], stop_reason="max_tokens"))
res = client.post("/api/chat", json={"message": "hi"})
check("budget exhausted -> 502", res.status_code, 502)
check("budget exhausted message", "more room" in res.json()["detail"], True)

capture(main, reply([text_block("Partial answ")], stop_reason="max_tokens"))
res = client.post("/api/chat", json={"message": "hi"})
check("truncated reply still returned", res.json()["reply"], "Partial answ")

# --- tier routing --------------------------------------------------------
seen = capture(main, reply([text_block("free")]))
res = client.post("/api/chat", json={"user_id": "guest_tester", "message": "hi"})
check("guest uses free model", seen["model"], main.FREE_MODEL)
check("free model id is the dated snapshot", main.FREE_MODEL, "claude-haiku-4-5-20251001")
check("free model budget", seen["max_tokens"], main.FREE_MAX_TOKENS)
check("free model sends no effort", "output_config" in seen, False)
check("guest credits reported", res.json()["remaining_credits"], 19)

main.supabase = FakeSupabase({"subscription_tier": "pro", "credits_balance": 50})
seen = capture(main, reply([thinking_block(), text_block("pro")]))
res = client.post("/api/chat", json={"user_id": "real-user", "message": "hi"})
check("pro uses pro model", seen["model"], main.PRO_MODEL)
check("pro model budget", seen["max_tokens"], main.PRO_MAX_TOKENS)
check("pro model sends effort", seen.get("output_config"), {"effort": main.PRO_EFFORT})
check("pro credit deducted", res.json()["remaining_credits"], 49)
check("deduction written back", main.supabase.updated, {"credits_balance": 49})

main.supabase = FakeSupabase({"subscription_tier": "free", "credits_balance": 0})
check("no credits -> 403", client.post("/api/chat", json={"user_id": "u", "message": "hi"}).status_code, 403)

# --- boot-time engine self test ------------------------------------------
# TestClient only runs lifespan inside a context manager, so the suite never
# fires a real request by accident; call the self test directly instead.
probe = load()
probe.anthropic_client.with_options = lambda **kw: probe.anthropic_client
probe.anthropic_client.messages.create = api_error(anthropic.BadRequestError, 400)
probe.run_engine_selftest()
check("selftest catches 400", probe.engine_selftest["status"], "failed")
check("selftest names the status", "HTTP 400" in probe.engine_selftest["detail"], True)
check("selftest names the model", probe.FREE_MODEL in probe.engine_selftest["detail"], True)
check("failed selftest degrades health",
      TestClient(probe.app).get("/api/health").json()["status"], "degraded")

probe.anthropic_client.messages.create = lambda **kw: reply([text_block("pong")])
probe.run_engine_selftest()
check("selftest passes on success", probe.engine_selftest["status"], "ok")
check("passing selftest keeps health ok",
      TestClient(probe.app).get("/api/health").json()["status"], "ok")

# A self test must never be able to take the service down.
def explode(**kwargs):
    raise RuntimeError("network on fire")
probe.anthropic_client.messages.create = explode
probe.run_engine_selftest()
check("selftest survives unexpected errors", probe.engine_selftest["status"], "failed")
check("selftest still serves health",
      TestClient(probe.app).get("/api/health").status_code, 200)

skipped = load(api_key=None)
skipped.anthropic_client.with_options = lambda **kw: skipped.anthropic_client
skipped.run_engine_selftest()
check("selftest skipped without a credential", skipped.engine_selftest["status"], "skipped")

# --- report --------------------------------------------------------------
failed = 0
for name, got, want in results:
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
