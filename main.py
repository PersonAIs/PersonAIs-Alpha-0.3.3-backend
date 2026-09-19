import logging
import os
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

APP_VERSION = "0.4.1"

app = FastAPI(title=f"PersonAIs Alpha {APP_VERSION} Engine", version=APP_VERSION)

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
    """Normalise a secret pulled from the environment.

    Secrets pasted into a hosting dashboard routinely arrive wrapped in quotes
    or carrying a trailing newline. The Anthropic client accepts all of those
    without complaint at construction time and only fails much later, as a 401
    'API key is invalid' on the first real request, so strip them here.
    """
    if not raw:
        return ""
    return raw.strip().strip('"').strip("'").strip()


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

anthropic_client = anthropic.Anthropic(api_key=anthropic_api_key or None)

# Kept in one place so the tier router and the health probe cannot drift apart.
PRO_MODEL = "claude-fable-5"
FREE_MODEL = "claude-haiku-4-5"

ENGINE_UNAVAILABLE = (
    "The AI engine is not configured correctly. This is a server-side problem, "
    "not something you did — please try again later."
)


# 4. Data Validation Bouncers
class ChatRequest(BaseModel):
    user_id: str = "guest_tester"
    message: str


def extract_reply(message: anthropic.types.Message) -> str:
    """Pull the assistant's text out of a response.

    Responses are a *list* of content blocks, and the first one is not reliably
    the text: on models that think (the pro/ultra tier does, and cannot be
    turned off) a thinking block comes first, and a ThinkingBlock has no .text
    attribute at all. Select by block type rather than by position.
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
        raise HTTPException(
            status_code=502,
            detail="The engine returned an empty reply. Please try again.",
        )

    return text


@app.get("/api/health")
async def health_check():
    """Report configuration state without exposing any secret material.

    Lets a deploy be checked from a browser instead of by sending a chat and
    reading the failure.
    """
    return {
        "status": "ok" if not anthropic_key_problem else "degraded",
        "version": APP_VERSION,
        "anthropic_key_configured": has_anthropic_credential,
        "anthropic_key_status": anthropic_key_problem or "ok",
        "supabase_configured": bool(supabase_url and supabase_key),
        "models": {"free": FREE_MODEL, "pro": PRO_MODEL},
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

    if tier in ["pro", "ultra"]:
        active_model = PRO_MODEL
        system_prompt = "You are a highly intelligent, elite digital twin. NEVER refer to yourself as an AI."
    else:
        active_model = FREE_MODEL
        system_prompt = "You are a helpful digital twin. Keep responses concise. NEVER refer to yourself as an AI."

    try:
        ai_response = anthropic_client.messages.create(
            model=active_model,
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {"role": "user", "content": req.message}
            ]
        )
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
    except anthropic.APIStatusError as e:
        # Includes a bad model id (404) and provider-side 5xx. Logged in full,
        # reported to the caller without the provider's response body.
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
