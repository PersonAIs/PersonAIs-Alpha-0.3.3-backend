from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class CreditCheckRequest(BaseModel):
    user_id: str

@router.post("/check")
async def check_credits(req: CreditCheckRequest):
    """Fetches the user's current credit balance."""
    try:
        response = supabase.table("profiles").select("compute_credits").eq("id", req.user_id).execute()
        
        if not response.data:
             raise HTTPException(status_code=404, detail="User profile not found")
             
        credits = response.data[0].get("compute_credits", 0)
        return {"compute_credits": credits, "has_enough": credits > 0}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/deduct")
async def deduct_credit(req: CreditCheckRequest):
    """Deducts 1 credit after a successful AI generation."""
    try:
        # First check if they have credits
        user_data = supabase.table("profiles").select("compute_credits").eq("id", req.user_id).execute()
        current_credits = user_data.data[0].get("compute_credits", 0) if user_data.data else 0

        if current_credits <= 0:
            raise HTTPException(status_code=403, detail="Insufficient compute credits. Please purchase a refill.")

        # Deduct 1 credit
        new_balance = current_credits - 1
        supabase.table("profiles").update({
            "compute_credits": new_balance
        }).eq("id", req.user_id).execute()

        return {"status": "deducted", "remaining_credits": new_balance}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))