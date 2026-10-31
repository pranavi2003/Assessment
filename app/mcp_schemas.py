"""Pydantic contracts and explicit JSON Schemas for customer MCP tools."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class CustomerRecordArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: str = Field(pattern=r"^CUST-[A-Z0-9]{5}$")


class RefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: str = Field(pattern=r"^CUST-[A-Z0-9]{5}$")
    amount: float = Field(gt=0, allow_inf_nan=False)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10)]


CUSTOMER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "string", "pattern": "^CUST-[A-Z0-9]{5}$"}
    },
    "required": ["customer_id"],
    "additionalProperties": False,
}

REFUND_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "string", "pattern": "^CUST-[A-Z0-9]{5}$"},
        "amount": {"type": "number", "exclusiveMinimum": 0},
        "reason": {"type": "string", "minLength": 10},
    },
    "required": ["customer_id", "amount", "reason"],
    "additionalProperties": False,
}

CUSTOMER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "string"},
        "name": {"type": "string"},
        "status": {"type": "string"},
        "tier": {"type": "string"},
    },
    "required": ["customer_id", "name", "status", "tier"],
    "additionalProperties": False,
}

REFUND_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "customer_id": {"type": "string"},
        "amount": {"type": "number", "exclusiveMinimum": 0},
        "reason": {"type": "string"},
        "refund_id": {"type": "string"},
    },
    "required": ["accepted", "customer_id", "amount", "reason", "refund_id"],
    "additionalProperties": False,
}
