"""Behavioural tests for the chat engine.

Runs without a network connection or real credentials: the Anthropic client
and the Supabase client are both replaced with stubs. Run it directly:

    pip install -r requirements.txt -r requirements-dev.txt
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


def load(api_key=VALID_KEY, auth_token=None, workspace_id=None,
         model=None, max_tokens=None):
    """Import main.py fresh with a given environment."""
    sys.modules.pop("main", None)
    for var, value in (("ANTHROPIC_API_KEY", api_key),
                       ("ANTHROPIC_AUTH_TOKEN", auth_token),
                       ("ANTHROPIC_WORKSPACE_ID", workspace_id),
                       ("ANTHROPIC_MODEL", model),
                       ("CHAT_MAX_TOKENS", max_tokens)):
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


def api_error(cls, status, message="boom"):
    def raiser(**kwargs):
        raise cls(
            message=message,
            response=httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com")),
            body={"error": {"message": message}},
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


def stub_model_list(main, ids):
    """Make GET /v1/models return a fixed set of ids (or blow up if None)."""
    main.anthropic_client.with_options = lambda **kw: main.anthropic_client

    def fake_list(**kwargs):
        if ids is None:
            raise anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.anthropic.com"))
        return [types.SimpleNamespace(id=i) for i in ids]

    main.anthropic_client.models.list = fake_list


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

# The 0.4.1 failure: the model id reached the API and the API would not serve
# it. The reply has to name the model, or nobody can tell what to change.
main.anthropic_client.messages.create = api_error(
    anthropic.NotFoundError, 404, "model: claude-fable-5")
res = client.post("/api/chat", json={"message": "hi"})
detail = res.json()["detail"]
check("unknown model -> 502", res.status_code, 502)
check("unknown model does NOT invite a retry", "retrying will not help" in detail, True)
check("unknown model names the model", main.CHAT_MODEL in detail, True)
check("unknown model quotes the provider", "model: claude-fable-5" in detail, True)
check("error detail still hides the key", "sk-ant" not in detail, True)

# --- one model, every tier ------------------------------------------------
main = load()
client = TestClient(main.app, raise_server_exceptions=False)

check("default model is the dated haiku snapshot",
      main.CHAT_MODEL_CONFIGURED, "claude-haiku-4-5-20251001")
check("no pro model id exists any more", hasattr(main, "PRO_MODEL"), False)
check("no separate free model id either", hasattr(main, "FREE_MODEL"), False)
check("no per-tier effort setting", hasattr(main, "PRO_EFFORT"), False)

seen = capture(main, reply([text_block("free")]))
res = client.post("/api/chat", json={"user_id": "guest_tester", "message": "hi"})
check("guest uses the chat model", seen["model"], main.CHAT_MODEL)
check("guest budget", seen["max_tokens"], main.MAX_TOKENS)
check("guest sends no effort parameter", "output_config" in seen, False)
check("guest gets the free persona", seen["system"], main.SYSTEM_PROMPTS["free"])
check("guest credits reported", res.json()["remaining_credits"], 19)
check("model_used reported", res.json()["model_used"], main.CHAT_MODEL)

main.supabase = FakeSupabase({"subscription_tier": "pro", "credits_balance": 50})
seen = capture(main, reply([text_block("pro")]))
res = client.post("/api/chat", json={"user_id": "real-user", "message": "hi"})
check("pro uses the SAME model", seen["model"], main.CHAT_MODEL)
check("pro budget is the same", seen["max_tokens"], main.MAX_TOKENS)
check("pro sends no effort parameter", "output_config" in seen, False)
check("pro keeps its own persona", seen["system"], main.SYSTEM_PROMPTS["pro"])
check("pro credit deducted", res.json()["remaining_credits"], 49)
check("deduction written back", main.supabase.updated, {"credits_balance": 49})

main.supabase = FakeSupabase({"subscription_tier": "ultra", "credits_balance": 5})
seen = capture(main, reply([text_block("ultra")]))
client.post("/api/chat", json={"user_id": "real-user", "message": "hi"})
check("ultra uses the SAME model", seen["model"], main.CHAT_MODEL)

main.supabase = FakeSupabase({"subscription_tier": "free", "credits_balance": 0})
check("no credits -> 403", client.post("/api/chat", json={"user_id": "u", "message": "hi"}).status_code, 403)

# The model id and the token budget are both dashboard-settable, so a wrong
# one is a restart to fix rather than a deploy.
override = load(model="claude-sonnet-5", max_tokens="2048")
check("model overridable from env", override.CHAT_MODEL_CONFIGURED, "claude-sonnet-5")
check("budget overridable from env", override.MAX_TOKENS, 2048)
check("junk budget ignored", load(max_tokens="lots").MAX_TOKENS, 1024)
check("negative budget ignored", load(max_tokens="-5").MAX_TOKENS, 1024)
check("quoted model id sanitized", load(model='"claude-sonnet-5"\n').CHAT_MODEL_CONFIGURED, "claude-sonnet-5")

# --- model resolution at boot --------------------------------------------
main = load()
pick = main.pick_served_id

check("served id is used as-is",
      pick("claude-haiku-4-5-20251001", ["claude-haiku-4-5-20251001", "claude-opus-5"])["status"],
      "served")
check("served id stays untouched",
      pick("claude-haiku-4-5-20251001", ["claude-haiku-4-5-20251001"])["active"],
      "claude-haiku-4-5-20251001")
check("dated id falls back to the alias",
      pick("claude-haiku-4-5-20251001", ["claude-haiku-4-5"])["active"], "claude-haiku-4-5")
check("alias falls back to the newest snapshot",
      pick("claude-haiku-4-5", ["claude-haiku-4-5-20250101", "claude-haiku-4-5-20251001"])["active"],
      "claude-haiku-4-5-20251001")
check("a corrected id says so",
      pick("claude-haiku-4-5-20251001", ["claude-haiku-4-5"])["status"], "corrected")
unservable = pick("claude-fable-5", ["claude-haiku-4-5-20251001"])
check("unservable id flagged", unservable["status"], "not served")
check("unservable id names the fix", "ANTHROPIC_MODEL" in unservable["detail"], True)
check("unreadable model list is not a failure",
      pick("claude-haiku-4-5-20251001", None)["status"], "unverified")
check("unverified id is still used",
      pick("claude-haiku-4-5-20251001", None)["active"], "claude-haiku-4-5-20251001")

# resolve_model() wires that up to the live client and to /api/health.
main = load()
stub_model_list(main, ["claude-haiku-4-5-20251001", "claude-opus-5"])
main.resolve_model()
check("resolution keeps a servable id", main.CHAT_MODEL, "claude-haiku-4-5-20251001")
health = TestClient(main.app).get("/api/health").json()
check("health reports model status", health["model_status"], "served")
check("health lists servable models", health["served_models"],
      ["claude-haiku-4-5-20251001", "claude-opus-5"])
check("health reports one model for both tiers",
      health["models"], {"free": main.CHAT_MODEL, "pro": main.CHAT_MODEL})
check("healthy deploy reads ok", health["status"], "ok")

main = load(model="claude-fable-5")
stub_model_list(main, ["claude-haiku-4-5-20251001"])
main.resolve_model()
check("unservable model degrades health",
      TestClient(main.app).get("/api/health").json()["status"], "degraded")
res = TestClient(main.app, raise_server_exceptions=False).post("/api/chat", json={"message": "hi"})
check("unservable model refuses without a round trip", res.status_code, 502)
check("refusal names the model", "claude-fable-5" in res.json()["detail"], True)

main = load()
stub_model_list(main, None)
main.resolve_model()
check("unreachable model list does not break boot", main.model_status["status"], "unverified")
check("unreachable model list keeps chat available",
      TestClient(main.app).get("/api/health").json()["status"], "ok")

# --- workspace scoping ---------------------------------------------------
# An org-scoped key must name a workspace on every request. The header goes on
# the CLIENT, not on each call, so GET /v1/models carries it too — 0.4.2 sent
# it only on messages.create, which is why served_models came back empty.
def workspace_header(m):
    headers = m.anthropic_client.default_headers
    return {k.lower(): v for k, v in headers.items()}.get("anthropic-workspace-id")

scoped = load(workspace_id="wrkspc_abc123")
check("workspace header on the client", workspace_header(scoped), "wrkspc_abc123")
seen = capture(scoped, reply([text_block("ok")]))
TestClient(scoped.app).post("/api/chat", json={"message": "hi"})
check("workspace not duplicated per request", "workspace_id" in seen, False)
check("workspace shown on health",
      TestClient(scoped.app).get("/api/health").json()["anthropic_workspace_id_set"], True)

unscoped = load(workspace_id=None)
check("no workspace header when unset", workspace_header(unscoped), None)
check("workspace absent on health",
      TestClient(unscoped.app).get("/api/health").json()["anthropic_workspace_id_set"], False)

quoted = load(workspace_id='"wrkspc_xyz"\n')
check("workspace id sanitized", workspace_header(quoted), "wrkspc_xyz")

# --- remedies ------------------------------------------------------------
# The live 0.4.2 failure: an org-scoped key with no workspace id set. The
# health page has to say which dashboard field fixes it.
main = load()
WORKSPACE_400 = ("This API key is not scoped to a workspace, so this request must "
                 "include the anthropic-workspace-id header with the ID of the "
                 "workspace to use. Add the header, or use an API key that is "
                 "scoped to a workspace.")
remedy = main.describe_remedy(f"HTTP 400 calling {main.CHAT_MODEL}: {WORKSPACE_400}")
check("workspace 400 has a remedy", bool(remedy), True)
check("remedy names the variable", "ANTHROPIC_WORKSPACE_ID" in remedy, True)
check("remedy names the alternative key", "ANTHROPIC_API_KEY" in remedy, True)
check("bad key has a remedy",
      "API keys" in (main.describe_remedy("HTTP 401: API key is invalid.") or ""), True)
check("missing model has a remedy",
      "ANTHROPIC_MODEL" in (main.describe_remedy("HTTP 404: model: claude-fable-5") or ""), True)
check("unknown failure has no invented remedy",
      main.describe_remedy("HTTP 500: something exploded"), None)
check("no failure, no remedy", main.describe_remedy(None, None), None)

probe = load()
probe.anthropic_client.with_options = lambda **kw: probe.anthropic_client
probe.anthropic_client.messages.create = api_error(anthropic.BadRequestError, 400, WORKSPACE_400)
probe.run_engine_selftest()
health = TestClient(probe.app).get("/api/health").json()
check("workspace failure degrades health", health["status"], "degraded")
check("health carries the remedy", "ANTHROPIC_WORKSPACE_ID" in (health["remedy"] or ""), True)
check("provider sentence is not truncated mid-word",
      health["engine_selftest_detail"].endswith("scoped to a workspace."), True)
check("healthy deploy has no remedy",
      TestClient(load().app).get("/api/health").json()["remedy"], None)

# --- reply extraction ----------------------------------------------------
main = load()
client = TestClient(main.app, raise_server_exceptions=False)

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

# --- boot-time engine self test ------------------------------------------
# TestClient only runs lifespan inside a context manager, so the suite never
# fires a real request by accident; call the self test directly instead.
probe = load()
probe.anthropic_client.with_options = lambda **kw: probe.anthropic_client
probe.anthropic_client.messages.create = api_error(
    anthropic.NotFoundError, 404, "model: claude-fable-5")
probe.run_engine_selftest()
check("selftest catches an unservable model", probe.engine_selftest["status"], "failed")
check("selftest names the status", "HTTP 404" in probe.engine_selftest["detail"], True)
check("selftest names the model", probe.CHAT_MODEL in probe.engine_selftest["detail"], True)
check("selftest quotes the provider",
      "model: claude-fable-5" in probe.engine_selftest["detail"], True)
check("failed selftest degrades health",
      TestClient(probe.app).get("/api/health").json()["status"], "degraded")

seen = capture(probe, reply([text_block("ready.")]))
probe.run_engine_selftest()
check("selftest passes on success", probe.engine_selftest["status"], "ok")
check("selftest sends the real chat request", seen["model"], probe.CHAT_MODEL)
check("selftest uses the real budget", seen["max_tokens"], probe.MAX_TOKENS)
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

os.environ["ENGINE_SELFTEST"] = "off"
disabled = load()
disabled.run_engine_selftest()
check("selftest can be switched off", disabled.engine_selftest["status"], "disabled")
check("disabled selftest does not degrade health",
      TestClient(disabled.app).get("/api/health").json()["status"], "ok")
os.environ.pop("ENGINE_SELFTEST")

# --- report --------------------------------------------------------------
failed = 0
for name, got, want in results:
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
