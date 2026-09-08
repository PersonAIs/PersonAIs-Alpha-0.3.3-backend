import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
import anthropic

# 1. Load environment variables
load_dotenv()

app = FastAPI(title="PersonAIs Alpha 0.4.0 Engine", version="0.4.0")

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

# 3. Initialize Cloud Vaults & AI Clients
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    print("🛑 CRITICAL ERROR: Supabase keys missing. Check your .env file!")

supabase: Client = create_client(supabase_url or "", supabase_key or "")
anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# 4. Data Validation Bouncers
class ChatRequest(BaseModel):
    user_id: str = "guest_tester"
    message: str

# --- THE CORE AI BRAIN & TIER ROUTER ---
@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
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
        active_model = "claude-fable-5" 
        system_prompt = "You are a highly intelligent, elite digital twin. NEVER refer to yourself as an AI."
    else:
        active_model = "claude-haiku-4-5-20251001" 
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
        reply_text = ai_response.content[0].text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Anthropic Engine Error: {str(e)}")

    new_balance = current_credits - 1
    if req.user_id != "guest_tester":
        supabase.table("profiles").update({"credits_balance": new_balance}).eq("id", req.user_id).execute()

    return {
        "reply": reply_text,
        "model_used": active_model,
        "remaining_credits": new_balance
    }