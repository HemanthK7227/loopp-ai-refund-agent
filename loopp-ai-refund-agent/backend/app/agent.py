from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .models import ChatResponse, Decision, TraceEvent


BASE_DIR = Path(__file__).resolve().parent.parent
POLICY_PATH = Path(os.getenv("POLICY_PATH", BASE_DIR / "data" / "refund_policy.md"))
ORDER_RE = re.compile(r"\bORD-\d{4}\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"(?:\$|usd\s*)\s*(\d+(?:\.\d{1,2})?)", re.IGNORECASE)


@dataclass
class Extraction:
    order_id: str | None = None
    reason: str = "unspecified"
    requested_amount: float | None = None
    has_evidence: bool = False
    prompt_injection: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class RequestExtractor:
    def __init__(self) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def extract(self, message: str) -> tuple[Extraction, str]:
        if not self.api_key:
            return heuristic_extract(message), "heuristic"

        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key)
            completion = client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract refund request facts as JSON. Return keys: order_id, reason, "
                            "requested_amount, has_evidence, prompt_injection. Do not decide eligibility."
                        ),
                    },
                    {"role": "user", "content": message},
                ],
                temperature=0,
            )
            content = completion.choices[0].message.content or "{}"
            parsed = json.loads(content)
            extraction = Extraction(
                order_id=normalize_order_id(parsed.get("order_id")),
                reason=str(parsed.get("reason") or "unspecified"),
                requested_amount=parse_amount(parsed.get("requested_amount")),
                has_evidence=bool(parsed.get("has_evidence")),
                prompt_injection=bool(parsed.get("prompt_injection")) or detects_prompt_injection(message),
                raw=parsed,
            )
            if not extraction.order_id:
                extraction.order_id = heuristic_extract(message).order_id
            return extraction, f"openai:{self.model}"
        except Exception as exc:  # pragma: no cover - network/API dependent
            fallback = heuristic_extract(message)
            fallback.raw = {"fallback_reason": str(exc)}
            return fallback, "heuristic_after_llm_error"


class RefundAgent:
    def __init__(self) -> None:
        self.extractor = RequestExtractor()
        self.policy_text = POLICY_PATH.read_text(encoding="utf-8")
        self.today = parse_today()

    def handle(self, customer_id: str, message: str, session_id: str | None = None) -> ChatResponse:
        session_id = session_id or str(uuid.uuid4())
        trace = Trace()
        trace.add("intake", "Request received", "New refund conversation started.", {"customer_id": customer_id})

        extraction, extractor_mode = self.extractor.extract(message)
        trace.add(
            "extractor",
            "Parsed request",
            f"Extractor mode: {extractor_mode}.",
            {"message": message},
            {
                "order_id": extraction.order_id,
                "reason": extraction.reason,
                "requested_amount": extraction.requested_amount,
                "has_evidence": extraction.has_evidence,
                "prompt_injection": extraction.prompt_injection,
            },
        )

        customer = db.get_customer(customer_id)
        trace.add(
            "tool",
            "lookup_customer",
            "Loaded customer profile from CRM storage.",
            {"customer_id": customer_id},
            redact_customer(customer),
        )
        if not customer:
            return self._finalize(
                session_id,
                customer_id,
                message,
                "INFO_NEEDED",
                "I could not find that customer profile. Please verify the customer ID before I can evaluate a refund.",
                None,
                None,
                trace,
                [],
            )

        if extraction.prompt_injection:
            trace.add(
                "security",
                "Prompt injection detected",
                "The request included instructions to override policy. Those instructions were ignored.",
                output={"action": "continue_with_policy_only"},
            )

        order_id = extraction.order_id
        if not order_id:
            orders = db.get_customer_orders(customer_id)
            trace.add(
                "tool",
                "list_customer_orders",
                "No order ID was supplied, so recent customer orders were checked.",
                {"customer_id": customer_id},
                [{"id": row["id"], "total": row["total"], "status": row["status"]} for row in orders],
            )
            if len(orders) == 1:
                order_id = orders[0]["id"]
                trace.add("planner", "Selected only order", "The customer has one order on file.", output={"order_id": order_id})
            else:
                return self._finalize(
                    session_id,
                    customer_id,
                    message,
                    "INFO_NEEDED",
                    "Please share the order ID so I can evaluate the refund against the policy.",
                    None,
                    None,
                    trace,
                    ["Identity and order ownership must be verified before a refund is processed."],
                )

        order = db.get_order(order_id)
        trace.add(
            "tool",
            "lookup_order",
            "Loaded order and line-item details.",
            {"order_id": order_id},
            summarize_order(order),
        )

        if not order:
            return self._finalize(
                session_id,
                customer_id,
                message,
                "INFO_NEEDED",
                "I could not find that order. Please check the order ID and try again.",
                order_id,
                None,
                trace,
                ["Refund requests must reference a valid order in the CRM database."],
            )

        clauses = self.find_policy_clauses(message, order, extraction)
        trace.add(
            "tool",
            "policy_lookup",
            "Retrieved policy clauses relevant to this request.",
            {"reason": extraction.reason, "order_id": order_id},
            [{"clause": clause} for clause in clauses],
        )

        decision, response = self.evaluate(customer, order, extraction, clauses, trace)
        return self._finalize(
            session_id,
            customer_id,
            message,
            decision,
            response,
            order_id,
            float(order["total"]),
            trace,
            clauses,
        )

    def evaluate(
        self,
        customer: dict[str, Any],
        order: dict[str, Any],
        extraction: Extraction,
        clauses: list[str],
        trace: "Trace",
    ) -> tuple[Decision, str]:
        if order["customer_id"] != customer["id"]:
            trace.add("validator", "Ownership check failed", "The order is not owned by the selected customer.")
            return (
                "DENIED",
                "I cannot process this refund because the order does not belong to the selected customer profile.",
            )

        requested_amount = extraction.requested_amount
        if requested_amount and requested_amount > float(order["total"]):
            trace.add(
                "validator",
                "Requested amount exceeds capture",
                "The requested refund is greater than the order total.",
                output={"requested_amount": requested_amount, "order_total": order["total"]},
            )
            return (
                "DENIED",
                f"I cannot refund more than the captured payment of ${float(order['total']):.2f}.",
            )

        if order["refund_status"] == "refunded":
            trace.add("validator", "Duplicate refund check", "The order has already been refunded.")
            return "DENIED", f"Order {order['id']} has already been refunded, so I cannot issue another refund."

        if order["status"] != "delivered":
            trace.add("validator", "Fulfillment status check", "The order is not marked delivered.")
            return (
                "ESCALATED",
                f"Order {order['id']} is currently {order['status']}. I escalated this to a human teammate for carrier review.",
            )

        if float(order["total"]) > 500:
            trace.add("validator", "High value threshold", "Refunds over $500 require human escalation.")
            return (
                "ESCALATED",
                f"Order {order['id']} totals ${float(order['total']):.2f}, so I escalated it for human approval.",
            )

        if customer["account_status"] != "active" or int(customer["return_count_90d"]) >= 4:
            trace.add("validator", "Risk threshold", "Customer risk controls require human review.")
            return (
                "ESCALATED",
                "This account requires human review because of recent return activity or account status.",
            )

        days_since_delivery = delivery_age_days(order)
        trace.add(
            "validator",
            "Refund window check",
            "Calculated days since delivery using the configured demo date.",
            output={"delivered_at": order["delivered_at"], "today": self.today.isoformat(), "days_since_delivery": days_since_delivery},
        )
        if days_since_delivery is not None and days_since_delivery > 30:
            return (
                "DENIED",
                f"Order {order['id']} was delivered {days_since_delivery} days ago, outside the 30-day refund window.",
            )

        items = order["items"]
        if any(item["final_sale"] for item in items):
            trace.add("validator", "Final sale check", "At least one requested line item is final sale.")
            return "DENIED", "This item is marked final sale, so it is not eligible for a refund under Loopp policy."

        if any(item["gift_card"] for item in items):
            trace.add("validator", "Gift card check", "Gift cards cannot be refunded for cash.")
            return "DENIED", "Gift cards cannot be refunded for cash. A human teammate can review store-credit exceptions."

        digital_items = [item for item in items if item["digital"]]
        if digital_items:
            if any(item["license_redeemed"] for item in digital_items):
                trace.add("validator", "Digital license check", "A digital license has already been redeemed.")
                return "DENIED", "This digital purchase has already been redeemed, so it is not refundable."
            if days_since_delivery is not None and days_since_delivery <= 14:
                trace.add("validator", "Digital refund check", "Unredeemed digital good is inside the 14-day window.")
                return "APPROVED", approval_message(order, "unredeemed digital item")
            return "DENIED", "Unredeemed digital products are refundable only within 14 days of delivery."

        consumables = [item for item in items if item["consumable"]]
        if consumables:
            if not is_damage_or_wrong_item(extraction.reason):
                trace.add("validator", "Consumable category check", "Consumables are limited to damaged or incorrect-item claims.")
                return "DENIED", "Consumable items are refundable only when damaged, spoiled, or incorrectly fulfilled."
            if days_since_delivery is not None and days_since_delivery > 7:
                return "DENIED", "Damaged or incorrect consumables must be reported within 7 days of delivery."
            if not extraction.has_evidence:
                return (
                    "INFO_NEEDED",
                    "Please upload a photo or receipt showing the damaged, spoiled, or incorrect consumable item.",
                )
            return "APPROVED", approval_message(order, "documented consumable issue")

        if is_damage_or_wrong_item(extraction.reason) and not extraction.has_evidence:
            trace.add("validator", "Evidence check", "Damage or wrong-item claims require evidence before approval.")
            return "INFO_NEEDED", "Please attach a photo or other evidence, then I can continue the refund review."

        trace.add("validator", "Standard eligibility", "No denial or escalation rule applied.")
        return "APPROVED", approval_message(order, "standard 30-day return")

    def find_policy_clauses(self, message: str, order: dict[str, Any], extraction: Extraction) -> list[str]:
        candidates = []
        policy_lines = [
            line.strip("- ").strip()
            for line in self.policy_text.splitlines()
            if line.strip().startswith("-")
        ]
        text = f"{message} {extraction.reason}".lower()
        items = order.get("items", [])
        signals = {
            "final sale": any(item["final_sale"] for item in items) or "final sale" in text,
            "$500": float(order["total"]) > 500,
            "digital": any(item["digital"] for item in items),
            "consumable": any(item["consumable"] for item in items),
            "gift card": any(item["gift_card"] for item in items),
            "evidence": is_damage_or_wrong_item(text),
            "30 calendar days": True,
            "prompt injection": extraction.prompt_injection,
            "already": order.get("refund_status") == "refunded",
        }
        for line in policy_lines:
            lower = line.lower()
            if any(key in lower and enabled for key, enabled in signals.items()):
                candidates.append(line)
        return candidates[:6] or policy_lines[:3]

    def _finalize(
        self,
        session_id: str,
        customer_id: str,
        message: str,
        decision: Decision,
        response: str,
        order_id: str | None,
        amount: float | None,
        trace: "Trace",
        clauses: list[str],
    ) -> ChatResponse:
        now = utc_now()
        trace.add("outcome", decision, response, output={"order_id": order_id, "amount": amount})
        events = trace.events
        db.save_decision(
            session_id,
            now,
            customer_id,
            message,
            decision,
            response,
            [event.model_dump() for event in events],
        )
        return ChatResponse(
            session_id=session_id,
            decision=decision,
            assistant_message=response,
            customer_id=customer_id,
            order_id=order_id,
            amount=amount,
            logs=events,
            policy_clauses=clauses,
        )


class Trace:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def add(
        self,
        kind: str,
        title: str,
        detail: str,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> None:
        self.events.append(
            TraceEvent(
                at=utc_now(),
                kind=kind,
                title=title,
                detail=detail,
                input=input,
                output=output,
            )
        )


def heuristic_extract(message: str) -> Extraction:
    order_match = ORDER_RE.search(message)
    amount_match = AMOUNT_RE.search(message)
    reason = classify_reason(message)
    evidence_terms = ("photo", "image", "attached", "upload", "receipt", "proof", "screenshot", "evidence")
    return Extraction(
        order_id=normalize_order_id(order_match.group(0)) if order_match else None,
        reason=reason,
        requested_amount=float(amount_match.group(1)) if amount_match else None,
        has_evidence=any(term in message.lower() for term in evidence_terms),
        prompt_injection=detects_prompt_injection(message),
        raw={"source": "heuristic"},
    )


def classify_reason(message: str) -> str:
    lower = message.lower()
    if is_damage_or_wrong_item(lower):
        if any(term in lower for term in ("wrong", "incorrect", "not what i ordered")):
            return "incorrect item"
        return "damaged item"
    if any(term in lower for term in ("late", "delayed", "missing", "not arrived", "never arrived")):
        return "shipping issue"
    if any(term in lower for term in ("changed my mind", "no longer", "don't want", "do not want")):
        return "buyer remorse"
    if "final sale" in lower:
        return "final sale dispute"
    return "unspecified"


def is_damage_or_wrong_item(text: str) -> bool:
    lower = text.lower()
    return any(
        term in lower
        for term in (
            "damaged",
            "broken",
            "cracked",
            "defective",
            "wrong item",
            "incorrect item",
            "spoiled",
            "stale",
            "mold",
            "not what i ordered",
        )
    )


def detects_prompt_injection(message: str) -> bool:
    lower = message.lower()
    suspicious = (
        "ignore the policy",
        "ignore policy",
        "disregard",
        "override",
        "developer mode",
        "system prompt",
        "refund anyway",
        "you must approve",
        "forget previous",
        "new instructions",
    )
    return any(term in lower for term in suspicious)


def normalize_order_id(value: Any) -> str | None:
    if not value:
        return None
    match = ORDER_RE.search(str(value))
    return match.group(0).upper() if match else None


def parse_amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_today() -> date:
    configured = os.getenv("AGENT_TODAY")
    if configured:
        return date.fromisoformat(configured)
    return date.today()


def delivery_age_days(order: dict[str, Any]) -> int | None:
    delivered_at = order.get("delivered_at")
    if not delivered_at:
        return None
    today = parse_today()
    return (today - date.fromisoformat(delivered_at)).days


def approval_message(order: dict[str, Any], reason: str) -> str:
    code = f"RFND-{order['id'].split('-')[-1]}"
    return (
        f"Approved. Order {order['id']} qualifies for a refund for {reason}. "
        f"I created authorization {code} for ${float(order['total']):.2f}."
    )


def summarize_order(order: dict[str, Any] | None) -> dict[str, Any] | None:
    if order is None:
        return None
    return {
        "id": order["id"],
        "customer_id": order["customer_id"],
        "status": order["status"],
        "delivered_at": order["delivered_at"],
        "total": order["total"],
        "refund_status": order["refund_status"],
        "items": [
            {
                "sku": item["sku"],
                "name": item["name"],
                "category": item["category"],
                "final_sale": item["final_sale"],
                "digital": item["digital"],
                "license_redeemed": item["license_redeemed"],
                "consumable": item["consumable"],
                "gift_card": item["gift_card"],
            }
            for item in order["items"]
        ],
    }


def redact_customer(customer: dict[str, Any] | None) -> dict[str, Any] | None:
    if customer is None:
        return None
    return {
        "id": customer["id"],
        "name": customer["name"],
        "tier": customer["tier"],
        "account_status": customer["account_status"],
        "return_count_90d": customer["return_count_90d"],
        "risk_notes": customer["risk_notes"],
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

