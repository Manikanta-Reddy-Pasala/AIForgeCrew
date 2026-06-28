"""api/main.py — FastAPI service exposing /health and /payments."""
from fastapi import FastAPI

from .routes import payments_router

app = FastAPI(title="api")
app.include_router(payments_router, prefix="/payments")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
