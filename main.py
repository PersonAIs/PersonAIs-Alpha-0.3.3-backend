import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
import anthropic
from openai import OpenAI

# 1. Load environment variables to unlock the vaults
load_dotenv()

app = FastAPI(title="PersonAIs Alpha 0.3.3 Engine")

# 2. CORS Security Bypass for Next.js Bridge
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], 
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
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 4. Data Validation Bouncers
class ChatRequest(BaseModel):
    user_id: str = "guest_tester" # Default fallback
    message: str

class AvatarSetupRequest(BaseModel):
    user_id: str = "guest_tester"
    description: str = None
    image_data: str = None 

# --- TASK 1: THE CORE AI BRAIN & TIER ROUTER ---
@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    # STEP 1: Intercept guest tester to prevent UUID database crashes
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

    # ... Keep the rest of your Gatekeeper and Claude Integration code exactly the same ...

    # Credit Gatekeeper
    if current_credits <= 0:
        raise HTTPException(
            status_code=403, 
            detail="Insufficient compute credits. Please upgrade to unlock unlimited chat."
        )

    # Dynamic Model Routing
    if tier in ["pro", "ultra"]:
        active_model = "claude-fable-5" 
        system_prompt = "You are a highly intelligent, elite digital twin. NEVER refer to yourself as an AI."
    else:
        active_model = "claude-haiku-4-5-20251001" 
        system_prompt = "You are a helpful digital twin. Keep responses concise. NEVER refer to yourself as an AI."

    # Claude Inference
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

    # Atomic Credit Deduction
    new_balance = current_credits - 1
    if req.user_id != "guest_tester":
        supabase.table("profiles").update({"credits_balance": new_balance}).eq("id", req.user_id).execute()

    return {
        "reply": reply_text,
        "model_used": active_model,
        "remaining_credits": new_balance
    }

# --- TASK 2: AVATAR GENERATION ROUTE ---
@app.post("/api/avatar/setup")
async def generate_digital_twin(req: AvatarSetupRequest):
    try:
        base_prompt = req.description if req.description else "A futuristic digital twin avatar based on the user's uploaded reference."
        enhanced_prompt = f"A highly detailed, futuristic cyberpunk digital twin avatar portrait. {base_prompt}. Cinematic lighting, 8k resolution, glassmorphic UI elements glowing in the background. High-end digital art style."

        # Call OpenAI's active image generation model
        response = openai_client.images.generate(
            model="gpt-image-1.5",
            prompt=enhanced_prompt,
            size="1024x1024",
            n=1,
        )
        
        new_avatar_url = response.data[0].url

        # Save to database if not a guest
        if req.user_id != "guest_tester":
            supabase.table("profiles").update({"avatar_url": new_avatar_url}).eq("id", req.user_id).execute()

        return {
            "success": True, 
            "avatar_url": new_avatar_url,
            "message": "Digital Twin successfully generated and saved."
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image Generation Failed: {str(e)}")