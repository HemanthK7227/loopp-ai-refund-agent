from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Decision = Literal["APPROVED", "DENIED", "ESCALATED", "INFO_NEEDED"]


class ChatRequest(BaseModel):
    customer_id: str = Field(..., examples=["C1001"])
    message: str = Field(..., min_length=1)
    session_id: str | None = None


class TraceEvent(BaseModel):
    at: str
    kind: str
    title: str
    detail: str
    input: dict[str, Any] | None = None
    output: dict[str, Any] | list[dict[str, Any]] | None = None


class ChatResponse(BaseModel):
    session_id: str
    decision: Decision
    assistant_message: str
    customer_id: str
    order_id: str | None
    amount: float | None = None
    logs: list[TraceEvent]
    policy_clauses: list[str] = Field(default_factory=list)


class CustomerSummary(BaseModel):
    id: str
    name: str
    email: str
    tier: str
    account_status: str
    return_count_90d: int
    risk_notes: str | None = None
    orders: list[dict[str, Any]]


class AdminLog(BaseModel):
    id: int
    session_id: str
    created_at: str
    customer_id: str
    message: str
    decision: Decision
    response: str
    logs: list[dict[str, Any]]

