from __future__ import annotations

import os

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .agent import RefundAgent
from .models import AdminLog, ChatRequest, ChatResponse, CustomerSummary


app = FastAPI(title="Loopp AI Refund Agent", version="1.0.0")

frontend_origin = os.getenv("FRONTEND_ORIGIN", "*")
origins = ["*"] if frontend_origin == "*" else [frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    db.init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/customers", response_model=list[CustomerSummary])
def customers() -> list[dict]:
    return db.list_customers()


@app.get("/api/admin/logs", response_model=list[AdminLog])
def admin_logs(limit: int = Query(default=25, ge=1, le=100)) -> list[dict]:
    return db.list_decisions(limit)


@app.get("/api/policy")
def policy() -> dict[str, str]:
    policy_path = os.getenv("POLICY_PATH", str(db.BASE_DIR / "data" / "refund_policy.md"))
    return {"text": open(policy_path, encoding="utf-8").read()}


@app.post("/api/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    agent = RefundAgent()
    return agent.handle(payload.customer_id, payload.message, payload.session_id)

