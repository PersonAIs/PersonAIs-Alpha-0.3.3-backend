import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
import anthropic

# 1. Load environment variables
load_dotenv()

logger = logging.getLogger("personais.engine")

APP_VERSION = "0.4.2"

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

# An API key that is not scoped to a workspace has to name one per request, or
# the API can reject the call. The SDK takes it as a request parameter
# (messages.create(workspace_id=...)) — 0.4.1 sent it as an 'anthropic-
# workspace-id' header instead, which the API does not read, so setting it had
# no effect. Only sent when configured; a workspace-scoped key needs nothing.
anthropic_workspace_id = clean_secret(os.getenv("ANTHROPIC_WORKSPACE_ID"))

anthropic_client = anthropic.Anthropic(api_key=anthropic_api_key or None)

# --- Model configuration -------------------------------------------------
#
# ONE model serves every tier. 0.4.1 routed pro/ultra accounts to a separate
# "claude-fable-5", which this account cannot serve: the API answers a model it
# will not serve with a 404, which reached the browser as the flat "The AI
# engine is not set up correctly" — a sentence that names neither the model nor
# the tier, which is why it looked like a key problem. Pro-tier accounts now
# get the same working model as everyone else. Do NOT reintroduce a second
# model id until that model is actually available on this API key; the boot
# check below is what proves it.
#
# The free-tier id was never the fault and is unchanged:
# "claude-haiku-4-5-20251001" is a real, servable id.
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

# 0.4.1 also sent output_config={"effort": ...} on the pro path. That parameter
# only applies to models that think; this model rejects it. With one model
# there is nothing to send it for, so it is gone — one less thing that can be
# rejected as malformed.

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
        return str(body.get("error", {}).get("message", ""))[:200]
    return ""


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


def build_request_kwargs(tier: str, message: str) -> dict:
    """Build the exact request body a tier sends.

    The boot self test calls this too, so a passing self test means the real
    chat request is accepted — not merely that the credential works. Anything
    that can be rejected (the model id, the token budget, the workspace id) is
    exercised by both paths or by neither.
    """
    kwargs = {
        "model": CHAT_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPTS.get(tier, SYSTEM_PROMPTS["free"]),
        "messages": [{"role": "user", "content": message}],
    }
    if anthropic_workspace_id:
        kwargs["workspace_id"] = anthropic_workspace_id
    return kwargs


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
        "anthropic_key_configured": has_anthropic_credential,
        "anthropic_key_status": anthropic_key_problem or "ok",
        "anthropic_workspace_id_set": bool(anthropic_workspace_id),
        "supabase_configured": bool(supabase_url and supabase_key),
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
    request_kwargs = build_request_kwargs(request_tier, req.message)
    active_model = request_kwargs["model"]

    # Boot already established this id is not servable, so the round trip can
    # only end one way. Say which model, so the fix is obvious.
    if model_status["status"] == "not served":
        logger.error("Refusing chat: %s", model_status["detail"])
        raise HTTPException(
            status_code=502,
            detail=misconfigured_detail(active_model, "this API key cannot serve that model"),
        )

    try:
        ai_response = anthropic_client.messages.create(**request_kwargs)
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

    reply_text = extract_reply(ai_response)

    new_balance = current_credits - 1
    if req.user_id != "guest_tester":
        supabase.table("profiles").update({"credits_balance": new_balance}).eq("id", req.user_id).execute()

    return {
        "reply": reply_text,
        "model_used": active_model,
        "remaining_credits": new_balance
    }
