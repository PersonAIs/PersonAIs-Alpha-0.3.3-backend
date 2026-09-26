import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
import anthropic

import social

# 1. Load environment variables
load_dotenv()

logger = logging.getLogger("personais.engine")

APP_VERSION = "0.4.5"

# Result of the boot-time self test, surfaced on /api/health.
engine_selftest = {"status": "not run", "detail": None}

# How the configured model id fared against the ids this key can actually
# serve. Filled in at boot by resolve_model(); surfaced on /api/health.
model_status = {
    "configured": None,
    "active": None,
    "status": "not run",
    "detail": None,
}

# Every model id this credential can serve. Surfaced on /api/health so a wrong
# id can be corrected from the dashboard without guessing at the right string.
served_models = []


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Resolution decides which model id /api/chat sends, so it has to finish
    # before the first request. It is one short GET, run off the event loop so
    # it cannot stall anything else that is starting up.
    await asyncio.get_running_loop().run_in_executor(None, resolve_model)
    # The self test sends a real message, so it is left on a worker thread and
    # not awaited: a slow API must never delay the port opening and fail the
    # hosting platform's own health check.
    asyncio.get_running_loop().run_in_executor(None, run_engine_selftest)
    # Whether the 0.4.4 social tables exist is a property of the database, not
    # of this process, so it is read once at boot and reported on /api/health
    # rather than discovered by the first person who tries to add a friend.
    asyncio.get_running_loop().run_in_executor(None, probe_social_schema)
    # Likewise the 0.4.5 table that daily discussion allowances are counted in.
    asyncio.get_running_loop().run_in_executor(None, probe_limits_schema)
    yield


app = FastAPI(
    title=f"PersonAIs Alpha {APP_VERSION} Engine",
    version=APP_VERSION,
    lifespan=lifespan,
)

# 2. Production CORS Security Bridge
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://personais.net",
        "https://www.personais.net"
    ], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def clean_secret(raw: Optional[str]) -> str:
    """Normalise a value pulled from the environment.

    Values pasted into a hosting dashboard routinely arrive wrapped in quotes
    or carrying a trailing newline. The Anthropic client accepts all of those
    without complaint at construction time and only fails much later, as a 401
    'API key is invalid' on the first real request, so strip them here.
    """
    if not raw:
        return ""
    return raw.strip().strip('"').strip("'").strip()


def positive_int(name: str, default: int) -> int:
    """Read a positive integer from the environment, or fall back.

    A typo in a dashboard field must not take the service down at import time,
    so a bad value is logged and ignored rather than raised.
    """
    raw = clean_secret(os.getenv(name))
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"⚠️  {name}={raw!r} is not a number — using {default}.")
        return default
    if value <= 0:
        print(f"⚠️  {name}={value} must be positive — using {default}.")
        return default
    return value


def daily_limit_setting(name: str, default: int) -> Optional[int]:
    """Read a daily allowance from the environment.

    A whole number is the allowance, and 0 means none at all — a way to pause
    what it meters without a deploy. 'off' removes the limit. Anything else
    keeps the default: a typo must never quietly turn a spending limit off.
    """
    raw = clean_secret(os.getenv(name))
    if not raw:
        return default
    if raw.lower() in ("off", "none", "unlimited"):
        return None
    try:
        value = int(raw)
    except ValueError:
        print(f"⚠️  {name}={raw!r} is not a number or 'off' — using {default}.")
        return default
    if value < 0:
        print(f"⚠️  {name}={value} cannot be negative — using {default}.")
        return default
    return value


# 3. Initialize Cloud Vaults & AI Clients
supabase_url = clean_secret(os.getenv("SUPABASE_URL"))
supabase_key = clean_secret(os.getenv("SUPABASE_KEY"))

if not supabase_url or not supabase_key:
    print("🛑 CRITICAL ERROR: Supabase keys missing. Check your .env file!")

supabase: Client = create_client(supabase_url or "", supabase_key or "")

anthropic_api_key = clean_secret(os.getenv("ANTHROPIC_API_KEY"))
# The SDK also authenticates with an OAuth bearer token. It is not how this
# service is normally deployed, but accepting it keeps a working setup working.
anthropic_auth_token = clean_secret(os.getenv("ANTHROPIC_AUTH_TOKEN"))
has_anthropic_credential = bool(anthropic_api_key or anthropic_auth_token)

if anthropic_auth_token and anthropic_auth_token != os.getenv("ANTHROPIC_AUTH_TOKEN"):
    # The client resolves the bearer token from the environment, so the
    # sanitised value has to go back there to be the one that gets used.
    os.environ["ANTHROPIC_AUTH_TOKEN"] = anthropic_auth_token


def describe_key_problem(key: str) -> Optional[str]:
    """Return a human-readable reason the key looks unusable, or None.

    A key that merely looks odd is reported and still tried, so this never
    invents a failure the API itself would not have raised.
    """
    if not key.startswith("sk-ant-"):
        return "ANTHROPIC_API_KEY does not start with 'sk-ant-' — check it is an API key, not an OAuth token or a placeholder"
    if len(key) < 40:
        return "ANTHROPIC_API_KEY looks truncated — confirm the whole value was copied"
    return None


if anthropic_api_key:
    anthropic_key_problem = describe_key_problem(anthropic_api_key)
elif anthropic_auth_token:
    anthropic_key_problem = None
else:
    anthropic_key_problem = "ANTHROPIC_API_KEY is missing or empty"
if anthropic_key_problem:
    # Printed at boot so the deploy log names the problem, instead of it only
    # surfacing as a 401 the first time somebody tries to chat.
    print(f"🛑 CRITICAL ERROR: {anthropic_key_problem}. /api/chat will refuse until this is fixed.")

# An API key that is NOT scoped to a workspace must name a workspace on every
# request, or the API rejects it with a 400 telling you to add the
# 'anthropic-workspace-id' header. This is what was actually breaking every
# chat message in 0.4.1 and 0.4.2: the mechanism was here, but
# ANTHROPIC_WORKSPACE_ID was never set in the environment, so nothing was sent.
#
# It goes on the client rather than on each request so that *every* endpoint
# carries it — including GET /v1/models, which the boot check uses and which
# 0.4.2 left unscoped (hence an empty served_models list).
#
# A workspace-scoped key needs none of this, so the header is only sent when
# the variable is set.
anthropic_workspace_id = clean_secret(os.getenv("ANTHROPIC_WORKSPACE_ID"))

anthropic_client = anthropic.Anthropic(
    api_key=anthropic_api_key or None,
    default_headers=(
        {"anthropic-workspace-id": anthropic_workspace_id}
        if anthropic_workspace_id
        else None
    ),
)

# --- Model configuration -------------------------------------------------
#
# ONE model serves every tier, and it is "claude-haiku-4-5-20251001" — a real,
# servable id that was never the fault.
#
# 0.4.1 routed pro/ultra accounts to a second id, "claude-fable-5". Whether
# this key can serve that model is still unknown: the boot check that would
# answer it (GET /v1/models) was itself blocked by the workspace-scoping 400
# described above, so served_models came back empty. It stays out until the
# health page proves it is available AND there is a reason to pay for it.
# Do NOT reintroduce a second model id before then.
#
# 0.4.1 also sent output_config={"effort": ...} on that same pro path. That
# parameter only applies to models that think; this one rejects it. With one
# model there is nothing to send it for, so it is gone — one less thing that
# can be rejected as malformed.
#
# Two defences so an unservable id cannot come back silently:
#   1. The id is overridable from the environment (ANTHROPIC_MODEL), so a
#      change is a dashboard edit and a restart, not a code deploy.
#   2. At boot it is checked against the ids this credential can actually serve
#      (GET /v1/models). A dated id is swapped for the bare one it matches (or
#      the reverse) automatically, and an id with no match at all is reported
#      on /api/health — with the list of ids that would work — instead of
#      waiting to fail a user.
CHAT_MODEL_CONFIGURED = clean_secret(os.getenv("ANTHROPIC_MODEL")) or "claude-haiku-4-5-20251001"

# Rebound by resolve_model() at boot; this is the fallback used when the model
# list cannot be fetched.
CHAT_MODEL = CHAT_MODEL_CONFIGURED

# Haiku does not produce thinking blocks, so the whole budget goes to the
# answer and 1024 tokens is enough for the concise replies this prompt asks
# for. Raise CHAT_MAX_TOKENS if replies start coming back truncated.
MAX_TOKENS = positive_int("CHAT_MAX_TOKENS", 1024)

SYSTEM_PROMPTS = {
    "pro": "You are a highly intelligent, elite digital twin. NEVER refer to yourself as an AI.",
    "free": "You are a helpful digital twin. Keep responses concise. NEVER refer to yourself as an AI.",
}

# Matches the "-20251001" on a dated snapshot id.
SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")

ENGINE_UNAVAILABLE = (
    "The AI engine is not configured correctly. This is a server-side problem, "
    "not something you did — please try again later."
)


def misconfigured_detail(model: str, reason: str) -> str:
    """The 'retrying will not help' message, naming the model and the reason.

    0.4.1 returned a bare sentence, which told nobody — including us — which
    model failed or why. Model ids and the provider's own parameter complaints
    are not secret; the key never appears here.
    """
    return (
        "The AI engine is not set up correctly, so this message could not be "
        f"answered ({model}: {reason}). It has been logged for us to fix — "
        "retrying will not help."
    )


def provider_message(e: anthropic.APIStatusError) -> str:
    """The provider's own explanation, truncated.

    It describes configuration — a model id, a rejected parameter — never the
    credential, so it is safe to show.
    """
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        return str(body.get("error", {}).get("message", ""))[:400]
    return ""


# The provider's sentence says what is wrong. These say where to fix it, so a
# failed boot check names the dashboard field to change instead of leaving the
# next person to work it out. Matched in order, first hit wins.
REMEDIES = (
    ("not scoped to a workspace",
     "This API key is org-scoped, so every request must name a workspace. Either set "
     "ANTHROPIC_WORKSPACE_ID (Anthropic Console → Settings → Workspaces → open the "
     "workspace → its id starts with 'wrkspc_') and restart, or replace "
     "ANTHROPIC_API_KEY with a key created inside a workspace."),
    ("workspace",
     "Something about workspace scoping is wrong. Check ANTHROPIC_WORKSPACE_ID against "
     "the id in Anthropic Console → Settings → Workspaces, or use a workspace-scoped key."),
    ("api key is invalid",
     "The key was rejected. Issue a fresh one in Anthropic Console → Settings → API keys "
     "and replace ANTHROPIC_API_KEY."),
    ("credit balance",
     "The Anthropic account is out of credit. Top it up in Console → Settings → Billing."),
    ("permission",
     "The key is valid but not permitted to use this model. Use a key with access, or set "
     "ANTHROPIC_MODEL to an id from served_models."),
    ("model:",
     "That model is not available on this API key. Set ANTHROPIC_MODEL to an id from "
     "served_models and restart."),
)


def describe_remedy(*details: Optional[str]) -> Optional[str]:
    """Turn a failure detail into the specific thing to change, if we know it."""
    for detail in details:
        if not detail:
            continue
        lowered = detail.lower()
        for needle, remedy in REMEDIES:
            if needle in lowered:
                return remedy
    return None


def served_model_ids() -> Optional[list]:
    """Every model id this credential can serve, or None if it cannot be read."""
    if not has_anthropic_credential:
        return None
    try:
        client = anthropic_client.with_options(timeout=10.0, max_retries=1)
        ids = []
        for model in client.models.list(limit=100):
            ids.append(model.id)
            if len(ids) >= 500:  # defensive: never page forever
                break
        return ids
    except Exception as e:
        logger.warning("Could not list servable models: %s", type(e).__name__)
        return None


def pick_served_id(configured: str, served: Optional[list]) -> dict:
    """Choose the id to actually send for a configured model id."""
    if served is None:
        return {
            "configured": configured,
            "active": configured,
            "status": "unverified",
            "detail": "the model list could not be fetched; using the configured id as-is",
        }

    if configured in served:
        return {"configured": configured, "active": configured,
                "status": "served", "detail": None}

    # "claude-haiku-4-5-20251001" configured, "claude-haiku-4-5" served.
    bare = SNAPSHOT_SUFFIX.sub("", configured)
    if bare != configured and bare in served:
        return {"configured": configured, "active": bare, "status": "corrected",
                "detail": f"'{configured}' is not served by this API key; using '{bare}'"}

    # The reverse: a bare id configured, only dated snapshots served.
    snapshots = sorted(m for m in served if m.startswith(bare + "-") and SNAPSHOT_SUFFIX.search(m))
    if snapshots:
        newest = snapshots[-1]
        return {"configured": configured, "active": newest, "status": "corrected",
                "detail": f"'{configured}' is not served by this API key; using '{newest}'"}

    return {
        "configured": configured,
        "active": configured,
        "status": "not served",
        "detail": f"'{configured}' is not one of the {len(served)} models this API key can "
                  "serve — set ANTHROPIC_MODEL to an id from 'served_models' below and restart",
    }


def resolve_model() -> None:
    """Point the service at a model id the account can actually serve."""
    global CHAT_MODEL, served_models

    served = served_model_ids()
    served_models = sorted(served) if served is not None else []
    outcome = pick_served_id(CHAT_MODEL_CONFIGURED, served)
    model_status.update(outcome)
    CHAT_MODEL = outcome["active"]

    if outcome["status"] == "not served":
        logger.error("Configured model is unservable — %s", outcome["detail"])
        print(f"🛑 CRITICAL ERROR: {outcome['detail']}")
    elif outcome["detail"]:
        logger.warning("Model check — %s", outcome["detail"])
        print(f"⚠️  Model check: {outcome['detail']}")
    else:
        logger.info("Chat model %s is servable by this API key", CHAT_MODEL)


# 4. Data Validation Bouncers
class ChatRequest(BaseModel):
    user_id: str = "guest_tester"
    message: str


def request_body(system: str, content: str, max_tokens: Optional[int] = None) -> dict:
    """The exact request body this service sends, for any caller.

    One builder for every path — the chat endpoint, the boot self test and the
    twin turns in social.py — so that anything which can be rejected (the model
    id, the token budget, the workspace id) is exercised by all of them or by
    none of them.
    """
    return {
        "model": CHAT_MODEL,
        "max_tokens": max_tokens or MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }


def build_request_kwargs(tier: str, message: str) -> dict:
    """The request body for a chat message from a given tier."""
    return request_body(SYSTEM_PROMPTS.get(tier, SYSTEM_PROMPTS["free"]), message)


def extract_reply(message: anthropic.types.Message) -> str:
    """Pull the assistant's text out of a response.

    Responses are a *list* of content blocks, and the first one is not reliably
    the text: on a model that thinks, a thinking block comes first, and a
    ThinkingBlock has no .text attribute at all. The model in use today does
    not think, but selecting by block type rather than by position keeps this
    correct if that ever changes.
    """
    if message.stop_reason == "refusal":
        raise HTTPException(
            status_code=422,
            detail="The engine declined to answer that one. Try rephrasing your message.",
        )

    text = "\n".join(
        block.text for block in message.content
        if block.type == "text" and block.text
    ).strip()

    if not text:
        logger.error(
            "Empty reply from Anthropic (stop_reason=%s, blocks=%s)",
            message.stop_reason,
            [block.type for block in message.content],
        )
        if message.stop_reason == "max_tokens":
            raise HTTPException(
                status_code=502,
                detail="That question needed more room than the engine had. Try asking it more simply.",
            )
        raise HTTPException(
            status_code=502,
            detail="The engine returned an empty reply. Please try again.",
        )

    if message.stop_reason == "max_tokens":
        # Partial text beats no text, but record it — repeated hits mean
        # CHAT_MAX_TOKENS is set too low.
        logger.warning("Reply truncated by max_tokens (%d chars returned)", len(text))

    return text


def generate_reply(system: str, content: str, max_tokens: Optional[int] = None) -> str:
    """Send one message to the model and return the assistant's text.

    Every provider failure mode is mapped here, once: a rejected key, a model
    this key cannot serve, a malformed request, a rate limit, an unreachable
    API. It used to live inside /api/chat, which was fine while chat was the
    only caller — 0.4.4 added twin deliberation, and a second copy of this
    ladder is a second place for the mapping to drift.

    Raises HTTPException; never returns an error string, so nothing that calls
    it can accidentally bill a credit for a failure.
    """
    if not has_anthropic_credential:
        logger.error("Refusing to call the model: %s", anthropic_key_problem)
        raise HTTPException(status_code=503, detail=ENGINE_UNAVAILABLE)

    kwargs = request_body(system, content, max_tokens)
    active_model = kwargs["model"]

    # Boot already established this id is not servable, so the round trip can
    # only end one way. Say which model, so the fix is obvious.
    if model_status["status"] == "not served":
        logger.error("Refusing to call the model: %s", model_status["detail"])
        raise HTTPException(
            status_code=502,
            detail=misconfigured_detail(active_model, "this API key cannot serve that model"),
        )

    try:
        response = anthropic_client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        # The 401 that produced "Anthropic Engine Error: Error code: 401".
        # The key is present but rejected: rotated, revoked, or from another org.
        logger.exception("Anthropic rejected the API key")
        raise HTTPException(status_code=503, detail=ENGINE_UNAVAILABLE)
    except anthropic.PermissionDeniedError:
        logger.exception("Anthropic API key lacks permission for %s", active_model)
        raise HTTPException(status_code=503, detail=ENGINE_UNAVAILABLE)
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit hit on %s", active_model)
        raise HTTPException(
            status_code=429,
            detail="The engine is busy right now. Give it a moment and try again.",
        )
    except anthropic.APIConnectionError:
        logger.exception("Could not reach Anthropic")
        raise HTTPException(
            status_code=503,
            detail="Could not reach the AI engine. Please try again in a moment.",
        )
    except anthropic.NotFoundError as e:
        # This is what an unavailable model id looks like — the 0.4.1 failure.
        logger.exception("Anthropic does not serve model %s for this key", active_model)
        raise HTTPException(
            status_code=502,
            detail=misconfigured_detail(
                active_model,
                provider_message(e) or "no such model for this API key",
            ),
        )
    except anthropic.BadRequestError as e:
        # Malformed request: an unservable model id, a rejected parameter, or a
        # key that is not scoped to a workspace and was sent without one.
        logger.exception(
            "Anthropic rejected the request for model %s (workspace id %s)",
            active_model,
            "set" if anthropic_workspace_id else "NOT set",
        )
        raise HTTPException(
            status_code=502,
            detail=misconfigured_detail(
                active_model,
                provider_message(e) or "the request was rejected as malformed",
            ),
        )
    except anthropic.APIStatusError as e:
        # Anything left is provider-side (5xx) and genuinely worth retrying.
        logger.exception("Anthropic returned %s for model %s", e.status_code, active_model)
        raise HTTPException(
            status_code=502,
            detail="The AI engine returned an error. Please try again in a moment.",
        )

    return extract_reply(response)


# --- Who is calling ------------------------------------------------------
#
# /api/chat has always trusted the user_id in its body, which is survivable
# while the only thing that id buys you is your own credit balance. Friends and
# shared discussions are private to two people, so from 0.4.4 the social
# endpoints resolve the caller from the Supabase access token instead and
# ignore any id sent in the body.
#
# SOCIAL_REQUIRE_AUTH=off falls back to the body id. That is for local
# development against a project with no auth set up; it makes every discussion
# readable by anyone who can guess a uuid, so it must not be set in production.
SOCIAL_REQUIRE_AUTH = clean_secret(
    os.getenv("SOCIAL_REQUIRE_AUTH") or "on"
).lower() not in ("0", "off", "false", "no")

SESSION_EXPIRED = "Your session could not be verified. Sign out and sign in again."


def bearer_token(authorization: Optional[str]) -> str:
    """The token out of an 'Authorization: Bearer <token>' header."""
    if not authorization:
        return ""
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def identify_user(authorization: Optional[str] = None,
                  claimed_user_id: Optional[str] = None) -> str:
    """Resolve the caller's user id, from their token where there is one."""
    token = bearer_token(authorization)
    if token:
        try:
            result = supabase.auth.get_user(token)
        except Exception as e:
            logger.warning("Rejected a session token: %s", type(e).__name__)
            raise HTTPException(status_code=401, detail=SESSION_EXPIRED)
        user = getattr(result, "user", None)
        if user is None and isinstance(result, dict):
            user = result.get("user")
        user_id = getattr(user, "id", None)
        if user_id is None and isinstance(user, dict):
            user_id = user.get("id")
        if not user_id:
            raise HTTPException(status_code=401, detail=SESSION_EXPIRED)
        return str(user_id)

    if SOCIAL_REQUIRE_AUTH:
        raise HTTPException(
            status_code=401,
            detail="This request carried no session. Sign in and try again.",
        )
    if not claimed_user_id or claimed_user_id == "guest_tester":
        raise HTTPException(
            status_code=401,
            detail="Friends and shared discussions need a signed-in account.",
        )
    return claimed_user_id


# --- Social feature configuration ----------------------------------------
#
# A deliberation round is one turn from each twin and costs each of them one
# credit, so a balance is what actually stops a pair of twins that will not
# agree. The cap is a backstop against a large balance and an endless argument;
# the per-request limit keeps one HTTP call short enough to survive a proxy.
DELIBERATION_ROUND_CAP = positive_int("DELIBERATION_ROUND_CAP", 50)
DELIBERATION_ROUNDS_PER_REQUEST = positive_int("DELIBERATION_ROUNDS_PER_REQUEST", 1)
TWIN_MAX_TOKENS = positive_int("TWIN_MAX_TOKENS", 512)

# Since 0.4.5: how many credits one person may spend on discussions per UTC
# day. A round costs each side one, so the default of three is three rounds a
# day for each of you. The balance still has to cover them; this caps how fast
# it can be spent on twins that will not agree.
DISCUSSION_DAILY_CREDIT_LIMIT = daily_limit_setting("DISCUSSION_DAILY_CREDIT_LIMIT", 3)


def now_utc() -> datetime:
    """The clock daily allowances are counted against.

    Looked up by name on every request, so a test can move it to tomorrow.
    """
    return datetime.now(timezone.utc)


# Whether this database has had the 0.4.4 migration run against it. Surfaced on
# /api/health next to the engine checks.
social_status = {"status": "not run", "detail": None}

# The same, for the 0.4.5 table the daily allowance is counted in.
limits_status = {"status": "not run", "detail": None}


def probe_social_schema() -> None:
    """Check the 0.4.4 tables exist, and say which migration to run if not."""
    if not (supabase_url and supabase_key):
        social_status.update(status="unknown", detail="Supabase is not configured")
        return
    try:
        # The second read covers a half-applied migration: the tables can exist
        # from an earlier attempt while the column the verdict loop writes does
        # not.
        supabase.table("friendships").select("id").limit(1).execute()
        supabase.table("conversation_members").select("verdict_note").limit(1).execute()
    except Exception as e:
        if social.looks_like_missing_schema(e):
            social_status.update(status="missing", detail=social.MIGRATION_REMEDY)
            logger.error("Social schema missing — %s", e)
            print(f"🛑 CRITICAL ERROR: {social.MIGRATION_REMEDY}")
        else:
            social_status.update(
                status="unknown",
                detail=f"{type(e).__name__} reading the social tables",
            )
            logger.warning("Could not verify the social schema: %s", type(e).__name__)
        return
    social_status.update(status="ready", detail=None)
    logger.info("Social schema is present")


def probe_limits_schema() -> None:
    """Check the 0.4.5 daily-limit table exists, and say which file adds it."""
    if DISCUSSION_DAILY_CREDIT_LIMIT is None:
        # Nothing is counted with the limit off, so there is nothing to need.
        limits_status.update(status="off", detail=None)
        return
    if not (supabase_url and supabase_key):
        limits_status.update(status="unknown", detail="Supabase is not configured")
        return
    try:
        supabase.table("discussion_usage").select("id").limit(1).execute()
    except Exception as e:
        if social.looks_like_missing_schema(e):
            limits_status.update(status="missing", detail=social.LIMITS_REMEDY)
            logger.error("Daily-limit schema missing — %s", e)
            print(f"🛑 CRITICAL ERROR: {social.LIMITS_REMEDY}")
        else:
            limits_status.update(
                status="unknown",
                detail=f"{type(e).__name__} reading the daily-limit table",
            )
            logger.warning("Could not verify the daily-limit schema: %s", type(e).__name__)
        return
    limits_status.update(status="ready", detail=None)
    logger.info("Daily-limit schema is present")


# One short message per boot. Set ENGINE_SELFTEST=off to skip it.
ENGINE_SELFTEST_ENABLED = clean_secret(
    os.getenv("ENGINE_SELFTEST") or "on"
).lower() not in ("0", "off", "false", "no")


def run_engine_selftest() -> None:
    """Send one real message at boot so a misconfiguration is visible now.

    Every configuration fault in this service so far — a rejected key, an
    unscoped key, a model the account cannot serve — has only surfaced when
    somebody tried to chat, and then only as a generic message with the real
    cause buried in the logs. One message per boot, built by the same function
    /api/chat uses, turns each deploy into a self test whose verdict can be
    read straight off /api/health.

    It can never break startup: it runs off the request path, every failure
    path is caught, and the call is given a timeout with retries disabled.
    """
    if not has_anthropic_credential:
        engine_selftest.update(status="skipped", detail=anthropic_key_problem)
        return
    if not ENGINE_SELFTEST_ENABLED:
        engine_selftest.update(status="disabled", detail="ENGINE_SELFTEST is off")
        return

    kwargs = build_request_kwargs("free", "Reply with the single word: ready.")
    try:
        anthropic_client.with_options(timeout=45.0, max_retries=0).messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        # The provider's own wording is the single most useful thing here, so
        # it is surfaced rather than swallowed.
        detail = f"HTTP {e.status_code} calling {kwargs['model']}"
        explanation = provider_message(e)
        if explanation:
            detail = f"{detail}: {explanation}"
        engine_selftest.update(status="failed", detail=detail)
        logger.error("Engine self test FAILED — %s", detail)
        print(f"🛑 Engine self test FAILED — {detail}")
        remedy = describe_remedy(detail)
        if remedy:
            logger.error("Engine self test remedy — %s", remedy)
            print(f"👉 FIX: {remedy}")
    except Exception as e:
        detail = f"{type(e).__name__} calling {kwargs['model']}"
        engine_selftest.update(status="failed", detail=detail)
        logger.error("Engine self test FAILED — %s", detail)
        print(f"🛑 Engine self test FAILED — {detail}")
    else:
        engine_selftest.update(status="ok", detail=f"{kwargs['model']} answered")
        logger.info("Engine self test OK — %s answered", kwargs["model"])
        print(f"✅ Engine self test OK — {kwargs['model']} answered")


@app.get("/api/health")
async def health_check():
    """Report configuration state without exposing any secret material.

    Lets a deploy be checked from a browser instead of by sending a chat and
    reading the failure. 'status' is only "ok" when the key is usable, the
    model id is one this key can serve, and the boot self test got a real
    answer back from that model.
    """
    healthy = (
        not anthropic_key_problem
        and model_status["status"] != "not served"
        and engine_selftest["status"] != "failed"
        # Friends and shared discussions are part of this release, so a
        # database that never had the migration run is a degraded deploy even
        # though one-to-one chat still works on it.
        and social_status["status"] != "missing"
        # Without the 0.4.5 table twin rounds refuse to run, since an allowance
        # nobody can count is one nobody can enforce.
        and limits_status["status"] != "missing"
    )
    return {
        "status": "ok" if healthy else "degraded",
        "version": APP_VERSION,
        "model": CHAT_MODEL,
        "model_configured": CHAT_MODEL_CONFIGURED,
        "model_status": model_status["status"],
        "model_detail": model_status["detail"],
        "max_tokens": MAX_TOKENS,
        "engine_selftest": engine_selftest["status"],
        "engine_selftest_detail": engine_selftest["detail"],
        # The specific thing to change, when the failure is one we recognise.
        "remedy": describe_remedy(engine_selftest["detail"], model_status["detail"]),
        "anthropic_key_configured": has_anthropic_credential,
        "anthropic_key_status": anthropic_key_problem or "ok",
        "anthropic_workspace_id_set": bool(anthropic_workspace_id),
        "supabase_configured": bool(supabase_url and supabase_key),
        "social_schema": social_status["status"],
        "social_remedy": social_status["detail"],
        "social_auth_required": SOCIAL_REQUIRE_AUTH,
        # null when DISCUSSION_DAILY_CREDIT_LIMIT is off.
        "discussion_daily_credit_limit": DISCUSSION_DAILY_CREDIT_LIMIT,
        "limits_schema": limits_status["status"],
        "limits_remedy": limits_status["detail"],
        # Every tier shares one model until a pro model is available on this
        # key; kept in the response so the routing is visible, not implied.
        "models": {"free": CHAT_MODEL, "pro": CHAT_MODEL},
        "served_models": served_models,
    }


# --- THE CORE AI BRAIN & TIER ROUTER ---
@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    # Fail fast and clearly rather than paying a round trip to be told the
    # obvious, and never echo the provider's raw 401 body to the browser.
    if not has_anthropic_credential:
        logger.error("Refusing chat: %s", anthropic_key_problem)
        raise HTTPException(status_code=503, detail=ENGINE_UNAVAILABLE)

    # Intercept guest tester to prevent UUID database crashes
    if req.user_id == "guest_tester":
        tier = "free"
        current_credits = 20
    else:
        try:
            response = supabase.table("profiles").select("subscription_tier, credits_balance").eq("id", req.user_id).execute()
            if not response.data:
                tier = "free"
                current_credits = 20
            else:
                user_data = response.data[0]
                tier = user_data.get("subscription_tier", "free")
                current_credits = user_data.get("credits_balance", 0)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

    if current_credits <= 0:
        raise HTTPException(
            status_code=403, 
            detail="Insufficient compute credits. Please upgrade to unlock unlimited chat."
        )

    # The tier still selects the persona, but every tier uses the one model
    # this API key is known to serve.
    request_tier = "pro" if tier in ["pro", "ultra"] else "free"
    active_model = CHAT_MODEL
    reply_text = generate_reply(
        system=SYSTEM_PROMPTS.get(request_tier, SYSTEM_PROMPTS["free"]),
        content=req.message,
    )

    new_balance = current_credits - 1
    if req.user_id != "guest_tester":
        supabase.table("profiles").update({"credits_balance": new_balance}).eq("id", req.user_id).execute()

    return {
        "reply": reply_text,
        "model_used": active_model,
        "remaining_credits": new_balance
    }


# --- Friends and shared discussions (0.4.4) ------------------------------
#
# The router is built with its dependencies rather than importing them, which
# keeps social.py free of module-level state and — because these are callables
# resolved per request — means a test that swaps out the Supabase client or the
# model call sees that swap on the social endpoints too.
app.include_router(
    social.build_social_router(
        db=lambda: supabase,
        generate=lambda system, content, max_tokens=None: generate_reply(
            system=system, content=content, max_tokens=max_tokens
        ),
        identify=identify_user,
        round_cap=DELIBERATION_ROUND_CAP,
        rounds_per_request=DELIBERATION_ROUNDS_PER_REQUEST,
        twin_max_tokens=TWIN_MAX_TOKENS,
        daily_credit_limit=DISCUSSION_DAILY_CREDIT_LIMIT,
        clock=lambda: now_utc(),
    ),
    prefix="/api",
)
