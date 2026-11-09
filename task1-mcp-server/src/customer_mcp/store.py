"""In-memory mock customer store.

A toy stand-in for a real customer/billing backend. Guarded by a single
`asyncio.Lock` so `get_customer_record` and `trigger_refund` -- which both
read, and the latter also writes, the same records -- never interleave
within one process. (Good enough for a single-process stdio server; it
is not a substitute for real transactional storage.)
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field


@dataclass
class RefundRecord:
    refund_id: str
    amount: float
    reason: str


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    status: str
    balance: float
    refund_history: list[RefundRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "name": self.name,
            "email": self.email,
            "status": self.status,
            "balance": self.balance,
            "refund_history": [
                {"refund_id": r.refund_id, "amount": r.amount, "reason": r.reason} for r in self.refund_history
            ],
        }


def _seed_customers() -> dict[str, Customer]:
    seed = [
        ("CUST-00001", "Ava Thompson", "ava.thompson@example.com", "active", 250.00),
        ("CUST-00002", "Noah Patel", "noah.patel@example.com", "active", 1200.50),
        ("CUST-00003", "Liam Garcia", "liam.garcia@example.com", "suspended", 0.00),
        ("CUST-00004", "Olivia Chen", "olivia.chen@example.com", "active", 75.25),
        ("CUST-00005", "Emma Rossi", "emma.rossi@example.com", "closed", 500.00),
    ]
    return {cid: Customer(cid, name, email, status, balance) for cid, name, email, status, balance in seed}


class RefundExceedsBalanceError(Exception):
    """Raised when a refund amount exceeds the customer's refundable balance."""


class CustomerNotFoundError(Exception):
    """Raised when a customer_id has no matching record."""


class CustomerStore:
    """Thread-unsafe-but-asyncio-safe (via `_lock`) in-memory customer store."""

    def __init__(self) -> None:
        self._customers: dict[str, Customer] = _seed_customers()
        self._lock = asyncio.Lock()

    async def get(self, customer_id: str) -> dict[str, object]:
        async with self._lock:
            customer = self._customers.get(customer_id)
            if customer is None:
                raise CustomerNotFoundError(customer_id)
            return customer.to_dict()

    async def refund(self, customer_id: str, amount: float, reason: str) -> dict[str, object]:
        async with self._lock:
            customer = self._customers.get(customer_id)
            if customer is None:
                raise CustomerNotFoundError(customer_id)
            if amount > customer.balance:
                raise RefundExceedsBalanceError(
                    f"refund amount {amount} exceeds refundable balance {customer.balance}"
                )
            refund_id = str(uuid.uuid4())
            customer.refund_history.append(RefundRecord(refund_id=refund_id, amount=amount, reason=reason))
            customer.balance -= amount
            return {
                "refund_id": refund_id,
                "customer_id": customer_id,
                "amount": amount,
                "reason": reason,
                "remaining_balance": customer.balance,
            }
