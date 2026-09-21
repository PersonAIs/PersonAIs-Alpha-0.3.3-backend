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
         model=None, max_tokens=None, social_auth="off", round_cap=None,
         rounds_per_request=None):
    """Import main.py fresh with a given environment.

    Social endpoints default to SOCIAL_REQUIRE_AUTH=off here so that most tests
    can post a plain user_id; the tests that care about identity turn it back
    on and drive a stubbed Supabase auth client.
    """
    sys.modules.pop("main", None)
    for var, value in (("ANTHROPIC_API_KEY", api_key),
                       ("ANTHROPIC_AUTH_TOKEN", auth_token),
                       ("ANTHROPIC_WORKSPACE_ID", workspace_id),
                       ("ANTHROPIC_MODEL", model),
                       ("CHAT_MAX_TOKENS", max_tokens),
                       ("SOCIAL_REQUIRE_AUTH", social_auth),
                       ("DELIBERATION_ROUND_CAP", round_cap),
                       ("DELIBERATION_ROUNDS_PER_REQUEST", rounds_per_request)):
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

# --- friends, discussions and twin deliberation (0.4.4) ------------------
#
# The whole feature is exercised against an in-memory stand-in for Supabase
# and a scripted model, so a round of twin deliberation can be run — and its
# credit arithmetic checked — without a database or an API key.
import uuid  # noqa: E402

import deliberation  # noqa: E402
import social  # noqa: E402


class FakeQuery:
    """One chained Supabase query: .select().eq().order().limit().execute()."""

    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.op, self.payload = "select", None
        self.filters, self.order_by, self.max_rows = [], None, None

    def select(self, _columns="*"):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def in_(self, column, values):
        self.filters.append(("in", column, list(values)))
        return self

    def ilike(self, column, pattern):
        self.filters.append(("ilike", column, pattern))
        return self

    def order(self, column, desc=False):
        self.order_by = (column, desc)
        return self

    def limit(self, count):
        self.max_rows = count
        return self

    def _matches(self, row):
        for kind, column, value in self.filters:
            cell = row.get(column)
            if kind == "eq" and cell != value:
                return False
            if kind == "in" and cell not in value:
                return False
            if kind == "ilike":
                needle = str(value).replace("%", "").lower()
                if not str(cell or "").lower().startswith(needle):
                    return False
        return True

    def execute(self):
        if self.table_name in self.db.missing:
            raise RuntimeError(
                f'relation "public.{self.table_name}" does not exist'
            )
        rows = self.db.tables.setdefault(self.table_name, [])

        if self.op == "insert":
            payload = self.payload if isinstance(self.payload, list) else [self.payload]
            written = []
            for item in payload:
                row = dict(item)
                row.setdefault("id", str(uuid.uuid4()))
                self.db.clock += 1
                row.setdefault("created_at", f"2026-01-01T00:00:{self.db.clock:02d}Z")
                rows.append(row)
                written.append(dict(row))
            self.db.writes.append((self.table_name, "insert", written))
            return types.SimpleNamespace(data=written)

        selected = [row for row in rows if self._matches(row)]

        if self.op == "update":
            for row in selected:
                row.update(self.payload)
            self.db.writes.append((self.table_name, "update", dict(self.payload)))
            return types.SimpleNamespace(data=[dict(row) for row in selected])

        if self.order_by:
            column, desc = self.order_by
            selected.sort(key=lambda r: r.get(column) or "", reverse=desc)
        if self.max_rows is not None:
            selected = selected[: self.max_rows]
        return types.SimpleNamespace(data=[dict(row) for row in selected])


class FakeAuth:
    """supabase.auth.get_user(jwt) over a fixed token -> user id mapping."""

    def __init__(self, tokens):
        self.tokens = tokens

    def get_user(self, token):
        if token not in self.tokens:
            raise RuntimeError("invalid JWT")
        return types.SimpleNamespace(
            user=types.SimpleNamespace(id=self.tokens[token])
        )


class FakeDB:
    def __init__(self, tables=None, missing=(), tokens=None):
        self.tables = {name: [dict(r) for r in rows]
                       for name, rows in (tables or {}).items()}
        self.missing = set(missing)
        self.writes = []
        self.clock = 0
        self.auth = FakeAuth(tokens or {})

    def table(self, name):
        return FakeQuery(self, name)

    def rows(self, name):
        return self.tables.setdefault(name, [])

    def credits(self, user_id):
        return next(r["credits_balance"] for r in self.rows("profiles") if r["id"] == user_id)


def social_db(credits_a=20, credits_b=20, brief_a=None, tokens=None):
    return FakeDB({
        "profiles": [
            {"id": "user-a", "display_name": "Ada", "friend_code": "PA-AAAAAA",
             "subscription_tier": "free", "credits_balance": credits_a, "twin_brief": brief_a},
            {"id": "user-b", "display_name": "Ben", "friend_code": "PA-BBBBBB",
             "subscription_tier": "pro", "credits_balance": credits_b, "twin_brief": None},
        ],
        "friendships": [],
        "conversations": [],
        "conversation_members": [],
        "conversation_messages": [],
    }, tokens=tokens)


def script(main, texts):
    """Answer each model call with the next scripted reply, recording the call."""
    calls = []
    queue = list(texts)

    def fake_create(**kwargs):
        calls.append(kwargs)
        return reply([text_block(queue.pop(0) if queue else "PROPOSAL: anything")])

    main.anthropic_client.messages.create = fake_create
    return calls


def befriend(db, a="user-a", b="user-b"):
    db.rows("friendships").append({
        "id": "link-1", "requester_id": a, "addressee_id": b,
        "status": "accepted", "created_at": "2026-01-01T00:00:00Z",
        "responded_at": "2026-01-01T00:01:00Z",
    })


def start_discussion(client, topic="Where to hold the offsite"):
    return client.post("/api/social/conversations", json={
        "user_id": "user-a", "friend_id": "user-b", "topic": topic, "mode": "auto",
    })


# --- the policy, on its own -----------------------------------------------
check("proposal is read off the marker line",
      deliberation.extract_proposal("We differ on cost.\nPROPOSAL: Lisbon in May, £400 cap."),
      "Lisbon in May, £400 cap.")
check("the last proposal wins",
      deliberation.extract_proposal("PROPOSAL: first\nmore talk\nPROPOSAL: second"), "second")
check("a twin that forgets the marker still leaves a ballot",
      deliberation.extract_proposal("Just Lisbon then."), "Just Lisbon then.")
check("an empty turn leaves nothing to vote on", deliberation.extract_proposal(""), "")

check("the opener alternates", deliberation.speaking_order(["a", "b"], 1), ["a", "b"])
check("the opener alternates back", deliberation.speaking_order(["a", "b"], 2), ["b", "a"])

check("two agreements settle it", deliberation.verdict_outcome(["agree", "agree"]), "resolved")
check("one disagreement keeps them talking",
      deliberation.verdict_outcome(["agree", "disagree"]), "continue")
check("a disagreement does not wait for the other vote",
      deliberation.verdict_outcome(["pending", "disagree"]), "continue")
check("one vote is not a verdict", deliberation.verdict_outcome(["agree", "pending"]), "waiting")

# A round costs BOTH twins a credit, so the poorer participant is the limit.
check("rounds are limited by the poorer twin", deliberation.affordable_rounds([9, 2], 5, 50), (2, None))
check("rounds are limited by the request", deliberation.affordable_rounds([9, 9], 1, 50), (1, None))
check("an empty balance stops the twins",
      deliberation.affordable_rounds([4, 0], 3, 50), (0, "credits_exhausted"))
check("the runaway cap stops the twins too",
      deliberation.affordable_rounds([9, 9], 3, 0), (0, "round_cap"))
check("a short-but-nonzero budget is not a stop reason",
      deliberation.affordable_rounds([1, 1], 4, 50)[1], None)
check("no credits means no next round",
      deliberation.can_continue([1, 0], 50, "deliberating"), False)
check("an agreement means no next round",
      deliberation.can_continue([9, 9], 50, "resolved"), False)

closing = deliberation.twin_system_prompt("Ada", "Ben", closing=True)
opening = deliberation.twin_system_prompt("Ada", "Ben", closing=False)
check("a twin speaks as its owner", "You are the digital twin of Ada" in closing, True)
check("a twin is told who it is talking to", "Ben" in closing, True)
check("only the closing twin writes a proposal",
      (deliberation.PROPOSAL_MARKER in closing, deliberation.PROPOSAL_MARKER in opening),
      (True, False))
check("a twin never calls itself an AI", "NEVER refer to yourself as an AI" in opening, True)
check("a pro twin is pushed further",
      "risk or the dependency" in deliberation.twin_system_prompt("Ada", "Ben", tier="pro"), True)
check("standing instructions reach the twin",
      "budget is fixed" in deliberation.twin_system_prompt(
          "Ada", "Ben", twin_brief="The budget is fixed"), True)

transcript = deliberation.render_transcript(
    [{"author": "twin", "user_id": "user-a", "content": "Lisbon."},
     {"author": "human", "user_id": "user-b", "content": "Not Lisbon."},
     {"author": "system", "user_id": None, "content": "Credits ran out."}],
    {"user-a": "Ada", "user-b": "Ben"},
)
check("a twin turn is labelled by its owner", "Ada: Lisbon." in transcript, True)
check("a typed message is marked as typed", "Ben (typed directly): Not Lisbon." in transcript, True)
check("a service note is not attributed to either side",
      "System note: Credits ran out." in transcript, True)

turn = deliberation.turn_prompt(
    topic="Offsite", owner_name="Ada", partner_name="Ben", transcript=transcript,
    notes=["Keep it under £400"], objection="Lisbon is too far for the Berlin team",
)
check("the turn names the topic", "TOPIC: Offsite" in turn, True)
check("typed notes are handed over as instructions", "Keep it under £400" in turn, True)
check("a rejection is handed to the twin that must answer it",
      "too far for the Berlin team" in turn, True)
check("an opening turn says the table is empty",
      "you are opening" in deliberation.turn_prompt(
          topic="t", owner_name="Ada", partner_name="Ben", transcript=""), True)

# --- friends ---------------------------------------------------------------
main = load()
client = TestClient(main.app, raise_server_exceptions=False)
db = social_db()
main.supabase = db

me = client.get("/api/social/me?user_id=user-a").json()["profile"]
check("your own card carries your friend code", me["friend_code"], "PA-AAAAAA")
check("your own card carries your balance", me["credits"], 20)

# A profile that signup never created is made on first contact, with a code.
db.rows("profiles").append({"id": "user-c", "credits_balance": 20})
fresh = client.get("/api/social/me?user_id=user-c").json()["profile"]
check("a missing friend code is allocated", fresh["friend_code"].startswith("PA-"), True)
check("an allocated code is the documented length", len(fresh["friend_code"]), 9)
check("an allocated code avoids ambiguous characters",
      set(fresh["friend_code"][3:]) <= set(social.CODE_ALPHABET), True)

named = client.post("/api/social/me", json={
    "user_id": "user-c", "display_name": "  Cleo  ", "twin_brief": "Say no to Mondays"}).json()
check("a display name is trimmed", named["profile"]["display_name"], "Cleo")
check("a twin brief is stored", named["profile"]["twin_brief"], "Say no to Mondays")

found = client.post("/api/social/friends/search",
                    json={"user_id": "user-a", "query": "BBBBBB"}).json()["results"]
check("a bare code still finds the twin", found[0]["friend_code"], "PA-BBBBBB")
check("a stranger is searchable but not yet a friend", found[0]["relationship"], "none")
check("a name search finds them too",
      client.post("/api/social/friends/search",
                  json={"user_id": "user-a", "query": "Be"}).json()["results"][0]["id"], "user-b")
check("one character is not a search",
      client.post("/api/social/friends/search",
                  json={"user_id": "user-a", "query": "B"}).status_code, 400)

asked = client.post("/api/social/friends/request",
                    json={"user_id": "user-a", "friend_code": "PA-BBBBBB"}).json()
check("a request starts as pending", asked["status"], "pending")
check("asking twice does not ask twice",
      client.post("/api/social/friends/request",
                  json={"user_id": "user-a", "friend_code": "PA-BBBBBB"}).json()["status"],
      "already_requested")
check("you cannot befriend yourself",
      client.post("/api/social/friends/request",
                  json={"user_id": "user-a", "friend_code": "PA-AAAAAA"}).status_code, 400)
check("an unknown code is a 404",
      client.post("/api/social/friends/request",
                  json={"user_id": "user-a", "friend_code": "PA-ZZZZZZ"}).status_code, 404)

pending = client.get("/api/social/friends?user_id=user-b").json()
check("the other side sees it waiting for them", len(pending["incoming"]), 1)
check("the sender sees it as sent", len(client.get("/api/social/friends?user_id=user-a")
                                       .json()["outgoing"]), 1)
request_id = pending["incoming"][0]["request_id"]

check("only the person asked can answer",
      client.post("/api/social/friends/respond",
                  json={"user_id": "user-a", "request_id": request_id,
                        "action": "accept"}).status_code, 403)
check("accepting works",
      client.post("/api/social/friends/respond",
                  json={"user_id": "user-b", "request_id": request_id,
                        "action": "accept"}).json()["status"], "accepted")
after = client.get("/api/social/friends?user_id=user-a").json()
check("an accepted request becomes a friend", [f["id"] for f in after["friends"]], ["user-b"])
check("and leaves nothing pending", (after["incoming"], after["outgoing"]), ([], []))
check("an answered request cannot be re-answered into something else",
      client.post("/api/social/friends/respond",
                  json={"user_id": "user-b", "request_id": request_id,
                        "action": "decline"}).json()["status"], "accepted")

# Asking somebody who has already asked you is an acceptance, not a second row.
db2 = social_db()
main.supabase = db2
client.post("/api/social/friends/request", json={"user_id": "user-a", "friend_code": "PA-BBBBBB"})
crossed = client.post("/api/social/friends/request",
                      json={"user_id": "user-b", "friend_code": "PA-AAAAAA"}).json()
check("crossed requests accept each other", crossed["status"], "accepted")
check("crossed requests leave one friendship", len(db2.rows("friendships")), 1)

# --- discussions ----------------------------------------------------------
main = load()
client = TestClient(main.app, raise_server_exceptions=False)
db = social_db()
main.supabase = db

check("a stranger cannot be dragged into a discussion",
      start_discussion(client).status_code, 403)

befriend(db)
created = start_discussion(client)
conversation = created.json()["conversation"]
conversation_id = conversation["id"]
check("a discussion opens with a friend", created.status_code, 200)
check("a discussion knows who else is in it", conversation["partner"]["display_name"], "Ben")
check("a discussion opens on round zero", conversation["round"], 0)
check("a discussion opens with no proposal", conversation["proposal"], None)
check("a topic is required",
      client.post("/api/social/conversations",
                  json={"user_id": "user-a", "friend_id": "user-b", "topic": "hi"}).status_code, 400)
check("an outsider cannot read it",
      client.get(f"/api/social/conversations/{conversation_id}?user_id=user-c").status_code, 403)

# Typing is the traditional mode: no model call, no credit.
typed = client.post(f"/api/social/conversations/{conversation_id}/messages",
                    json={"user_id": "user-a", "content": "Berlin team can't fly far."})
check("a typed message posts", typed.status_code, 200)
check("a typed message is attributed to the human", typed.json()["message"]["author"], "human")
check("typing costs nothing", db.credits("user-a"), 20)
check("an empty message is refused",
      client.post(f"/api/social/conversations/{conversation_id}/messages",
                  json={"user_id": "user-a", "content": "   "}).status_code, 400)
check("there is nothing to vote on before a round",
      client.post(f"/api/social/conversations/{conversation_id}/verdict",
                  json={"user_id": "user-a", "verdict": "agree"}).status_code, 409)

# --- one round of deliberation --------------------------------------------
calls = script(main, ["Berlin, two nights.",
                      "Agreed on Berlin.\nPROPOSAL: Berlin, 12-14 May, £400 each."])
round_one = client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                        json={"user_id": "user-a"}).json()
check("a round is two turns", len(round_one["messages"]), 2)
check("a round is two model calls", len(calls), 2)
check("the twins speak as twins", {m["author"] for m in round_one["messages"]}, {"twin"})
check("the round is counted", round_one["conversation"]["round"], 1)
check("the closing twin's proposal is what gets voted on",
      round_one["conversation"]["proposal"], "Berlin, 12-14 May, £400 each.")
check("each twin's owner pays for their own turn",
      (db.credits("user-a"), db.credits("user-b")), (19, 19))
check("a twin turn gets the twin budget", calls[0]["max_tokens"], main.TWIN_MAX_TOKENS)
check("the opening twin is not asked for a proposal",
      deliberation.PROPOSAL_MARKER in calls[0]["system"], False)
check("the closing twin is", deliberation.PROPOSAL_MARKER in calls[1]["system"], True)
check("the first twin is told what its owner typed",
      "Berlin team can't fly far." in calls[0]["messages"][0]["content"], True)
check("the second twin hears the first one",
      "Berlin, two nights." in calls[1]["messages"][0]["content"], True)
check("a pro friend's twin gets the pro persona",
      "risk or the dependency" in calls[1]["system"], True)
check("the discussion is now waiting on the humans",
      round_one["conversation"]["status"], "deliberating")
check("there is more where that came from", round_one["can_continue"], True)

# --- disagreement keeps the twins talking ---------------------------------
check("agreeing is recorded",
      client.post(f"/api/social/conversations/{conversation_id}/verdict",
                  json={"user_id": "user-a", "verdict": "agree"}).json()["outcome"], "waiting")
rejected = client.post(f"/api/social/conversations/{conversation_id}/verdict", json={
    "user_id": "user-b", "verdict": "disagree", "note": "£400 will not cover flights."}).json()
check("one disagreement reopens the discussion", rejected["outcome"], "continue")
check("a reopened discussion is ready for another round",
      rejected["conversation"]["status"], "open")
check("the objection is visible to both of you",
      "£400 will not cover flights." in rejected["messages"][0]["content"], True)
check("a rejected proposal can still be run again",
      rejected["conversation"]["can_deliberate"], True)
check("a verdict must be one of the two",
      client.post(f"/api/social/conversations/{conversation_id}/verdict",
                  json={"user_id": "user-a", "verdict": "maybe"}).status_code, 400)

calls = script(main, ["Raise it to £550.", "PROPOSAL: Berlin, 12-14 May, £550 each."])
round_two = client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                        json={"user_id": "user-b", "expected_round": 1}).json()
check("the objection briefs the twin that has to answer it",
      "£400 will not cover flights." in calls[0]["messages"][0]["content"], True)
# Opening a round is an advantage — the closer only reacts — so it alternates.
check("the second round opens with the other twin", calls[0]["system"].startswith(
    "You are the digital twin of Ben"), True)
check("a second round is counted", round_two["conversation"]["round"], 2)
check("a new proposal resets both verdicts",
      (round_two["conversation"]["my_verdict"], round_two["conversation"]["partner_verdict"]),
      ("pending", "pending"))
check("two rounds cost two credits each",
      (db.credits("user-a"), db.credits("user-b")), (18, 18))

check("a stale round number is refused rather than charged",
      client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                  json={"user_id": "user-a", "expected_round": 1}).status_code, 409)
check("the refusal charged nobody", (db.credits("user-a"), db.credits("user-b")), (18, 18))

# --- agreement ends it ----------------------------------------------------
client.post(f"/api/social/conversations/{conversation_id}/verdict",
            json={"user_id": "user-a", "verdict": "agree"})
settled = client.post(f"/api/social/conversations/{conversation_id}/verdict",
                      json={"user_id": "user-b", "verdict": "agree"}).json()
check("agreeing twice settles it", settled["outcome"], "resolved")
check("a settled discussion says so", settled["conversation"]["status"], "resolved")
check("a settled discussion stops the twins", settled["conversation"]["can_deliberate"], False)
check("the twins will not reopen it themselves",
      client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                  json={"user_id": "user-a"}).status_code, 409)

# --- the twins talk until the usage is up ---------------------------------
# Two credits each: two rounds, and then the balance decides it is over.
main = load()
client = TestClient(main.app, raise_server_exceptions=False)
db = social_db(credits_a=2, credits_b=2)
main.supabase = db
befriend(db)
conversation_id = start_discussion(client).json()["conversation"]["id"]
script(main, ["a1", "PROPOSAL: p1", "a2", "PROPOSAL: p2", "a3", "PROPOSAL: p3"])

first = client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                    json={"user_id": "user-a"}).json()
check("the first of two affordable rounds runs", first["conversation"]["round"], 1)
check("and knows another is affordable", first["can_continue"], True)

second = client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                     json={"user_id": "user-a"}).json()
check("the last affordable round runs", second["conversation"]["round"], 2)
check("the twins stop when the credits are gone", second["can_continue"], False)
check("and say why", second["stop_reason"], "credits_exhausted")
check("the discussion is marked exhausted", second["conversation"]["status"], "exhausted")
check("both balances are spent", (db.credits("user-a"), db.credits("user-b")), (0, 0))
check("the transcript records the stop",
      "credits ran out" in second["messages"][-1]["content"], True)

exhausted = client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                        json={"user_id": "user-a"}).json()
check("asking again runs nothing", exhausted["rounds_run"], 0)
check("asking again charges nothing", (db.credits("user-a"), db.credits("user-b")), (0, 0))
check("asking again repeats the reason", exhausted["stop_reason"], "credits_exhausted")
check("a spent discussion can still be typed in",
      client.post(f"/api/social/conversations/{conversation_id}/messages",
                  json={"user_id": "user-a", "content": "Let's just decide ourselves."}
                  ).status_code, 200)

# One side out of credits stops the round: a round needs a turn from each twin.
main = load()
client = TestClient(main.app, raise_server_exceptions=False)
db = social_db(credits_a=10, credits_b=0)
main.supabase = db
befriend(db)
conversation_id = start_discussion(client).json()["conversation"]["id"]
calls = script(main, ["never sent"])
broke = client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                    json={"user_id": "user-a"}).json()
check("a round needs both twins to be able to pay", broke["rounds_run"], 0)
check("the solvent twin is not charged for a round that cannot run",
      db.credits("user-a"), 10)
check("and the model is never called", len(calls), 0)

# The runaway cap is a backstop, and it is reported as its own reason.
main = load(round_cap="1")
client = TestClient(main.app, raise_server_exceptions=False)
db = social_db()
main.supabase = db
befriend(db)
conversation_id = start_discussion(client).json()["conversation"]["id"]
script(main, ["a1", "PROPOSAL: p1"])
client.post(f"/api/social/conversations/{conversation_id}/deliberate", json={"user_id": "user-a"})
capped = client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                     json={"user_id": "user-a"}).json()
check("the runaway cap stops a well-funded argument", capped["stop_reason"], "round_cap")
check("the cap leaves the credits alone", db.credits("user-a"), 19)

# --- a failed model call must not bill a credit ---------------------------
main = load()
client = TestClient(main.app, raise_server_exceptions=False)
db = social_db()
main.supabase = db
befriend(db)
conversation_id = start_discussion(client).json()["conversation"]["id"]
main.anthropic_client.messages.create = api_error(anthropic.RateLimitError, 429)
rate_limited = client.post(f"/api/social/conversations/{conversation_id}/deliberate",
                           json={"user_id": "user-a"})
check("a provider failure is passed through", rate_limited.status_code, 429)
check("a failed turn costs nothing", (db.credits("user-a"), db.credits("user-b")), (20, 20))
check("a failed round is not counted",
      client.get(f"/api/social/conversations/{conversation_id}?user_id=user-a")
      .json()["conversation"]["round"], 0)

# --- a database without the migration ------------------------------------
main = load()
client = TestClient(main.app, raise_server_exceptions=False)
main.supabase = FakeDB({"profiles": []}, missing={"friendships", "conversation_members"})
missing = client.get("/api/social/friends?user_id=user-a")
check("a missing migration is a 503, not a 500", missing.status_code, 503)
check("a missing migration names the fix",
      "Alpha 0.4.4" in missing.json()["detail"], True)
main.probe_social_schema()
check("the boot probe notices it too", main.social_status["status"], "missing")
check("and degrades the health page",
      TestClient(main.app).get("/api/health").json()["status"], "degraded")
check("the health page carries the migration remedy",
      "SQL" in (TestClient(main.app).get("/api/health").json()["social_remedy"] or ""), True)

main = load()
main.supabase = social_db()
main.probe_social_schema()
check("a migrated database reads ready", main.social_status["status"], "ready")
check("and keeps the health page ok",
      TestClient(main.app).get("/api/health").json()["status"], "ok")

# --- identity -------------------------------------------------------------
# With auth on — the production default — the caller is whoever the access
# token says they are, and the body cannot claim otherwise.
main = load(social_auth="on")
client = TestClient(main.app, raise_server_exceptions=False)
db = social_db(tokens={"good-token": "user-b"})
main.supabase = db
check("auth is on by default", main.SOCIAL_REQUIRE_AUTH, True)
check("no token, no friends", client.get("/api/social/friends").status_code, 401)
check("a bad token is rejected",
      client.get("/api/social/friends",
                 headers={"Authorization": "Bearer nope"}).status_code, 401)
check("a token identifies the caller",
      client.get("/api/social/friends",
                 headers={"Authorization": "Bearer good-token"}).json()["me"]["id"], "user-b")
check("a claimed user_id cannot override the token",
      client.get("/api/social/friends?user_id=user-a",
                 headers={"Authorization": "Bearer good-token"}).json()["me"]["id"], "user-b")
check("a bearer prefix is required",
      client.get("/api/social/friends",
                 headers={"Authorization": "good-token"}).status_code, 401)
check("the health page says whether auth is enforced",
      TestClient(main.app).get("/api/health").json()["social_auth_required"], True)

check("guests have no social account",
      TestClient(load(social_auth="off").app, raise_server_exceptions=False)
      .get("/api/social/friends?user_id=guest_tester").status_code, 401)

# Chat is untouched by all of this: it still answers a guest without a token.
main = load()
main.supabase = FakeSupabase(None)
capture(main, reply([text_block("still here")]))
check("one-to-one chat still works for a guest",
      TestClient(main.app).post("/api/chat",
                                json={"user_id": "guest_tester", "message": "hi"}).status_code, 200)

# --- report --------------------------------------------------------------
failed = 0
for name, got, want in results:
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
