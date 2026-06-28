"""api/routes.py — payment endpoints."""
from fastapi import APIRouter

payments_router = APIRouter()


@payments_router.post("/process")
def process_payment(amount: float) -> dict:
    return {"processed": amount}
