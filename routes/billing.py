"""Stripe checkout and webhook endpoints.

NOTE — unresolved schema mismatch. This module reads and writes the profiles
column `compute_credits`, but the live chat path in main.py uses
`credits_balance`. Only one of them matches the real Supabase table.

Nothing breaks today because this router is never registered on the app (see
main.py — there is no include_router call, and the pricing page deliberately
stubs checkout out with "Pre-orders are not open yet"). Before wiring payments
up, confirm the real column name and make both sides agree, or credits will be
read from one column and written to the other.
"""

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
import stripe
import os
from supabase import create_client, Client
from pydantic import BaseModel

# Initialize Router
router = APIRouter()

# Load Environment Variables
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Initialize Stripe & Supabase
stripe.api_key = STRIPE_SECRET_KEY
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Define Request Models
class CheckoutRequest(BaseModel):
    user_id: str
    product_type: str  # "alpha_pass" or "credit_pack"

@router.post("/create-checkout-session")
async def create_checkout_session(req: CheckoutRequest):
    """Generates a secure Stripe Checkout URL for the Next.js frontend."""
    try:
        # 1. Define Pricing based on the product type
        if req.product_type == "alpha_pass":
            price_amount = 500  # $5.00 CAD (or USD depending on your Stripe default)
            product_name = "PersonAIs Alpha Pass (Beta Pro/Ultra Perks)"
        elif req.product_type == "credit_pack":
            price_amount = 1000 # $10.00 for a compute credit refill
            product_name = "100 Compute Credits"
        else:
            raise HTTPException(status_code=400, detail="Invalid product type")

        # 2. Create the Stripe Session
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "cad", # Auto-settles to CAD to avoid tax/dilution issues
                    "product_data": {"name": product_name},
                    "unit_amount": price_amount,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url="http://localhost:3000/auth?success=true",
            cancel_url="http://localhost:3000/pricing?canceled=true",
            # Critical: Pass the user ID and product so the webhook knows who to upgrade
            metadata={
                "user_id": req.user_id,
                "product_type": req.product_type
            }
        )
        return {"url": session.url}

    except Exception as e:
        print(f"Checkout Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Listens for Stripe's 'Payment Successful' signal to update Supabase."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        # Verify the webhook is genuinely from Stripe
        event = stripe.Webhook.construct_event(
            payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET", "")
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Handle the successful payment
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")
        product_type = session.get("metadata", {}).get("product_type")

        if user_id:
            try:
                if product_type == "alpha_pass":
                    # Upgrade to Pro and give initial 50 credits
                    supabase.table("profiles").update({
                        "subscription_tier": "pro",
                        "compute_credits": 50
                    }).eq("id", user_id).execute()
                    print(f"✅ Upgraded user {user_id} to PRO")
                
                elif product_type == "credit_pack":
                    # Fetch current credits and add 100
                    user_data = supabase.table("profiles").select("compute_credits").eq("id", user_id).execute()
                    current_credits = user_data.data[0].get("compute_credits", 0) if user_data.data else 0
                    
                    supabase.table("profiles").update({
                        "compute_credits": current_credits + 100
                    }).eq("id", user_id).execute()
                    print(f"✅ Added 100 credits to user {user_id}")

            except Exception as e:
                print(f"Database Update Failed: {e}")

    return JSONResponse(content={"status": "success"})