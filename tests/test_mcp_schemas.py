import math

import pytest
from pydantic import ValidationError

from app.mcp_schemas import (
    CustomerRecordArgs, RefundArgs,
    CUSTOMER_INPUT_SCHEMA, REFUND_INPUT_SCHEMA,
)


@pytest.mark.parametrize("customer_id", ["CUST-12345", "CUST-A1B2C", "CUST-00000"])
def test_customer_id_valid_formats(customer_id):
    assert CustomerRecordArgs.model_validate({"customer_id": customer_id}).customer_id == customer_id


@pytest.mark.parametrize("customer_id", [
    "cust-12345", "CUST-1234", "CUST-123456", "CUST-ABCDE!", "CUST 12345", "", "12345",
])
def test_customer_id_invalid_formats(customer_id):
    with pytest.raises(ValidationError):
        CustomerRecordArgs.model_validate({"customer_id": customer_id})


def test_customer_rejects_extra_fields():
    with pytest.raises(ValidationError):
        CustomerRecordArgs.model_validate({"customer_id": "CUST-12345", "extra": True})


@pytest.mark.parametrize("amount", [0.01, 1, 1.0, 999999.99])
def test_refund_accepts_positive_amount(amount):
    result = RefundArgs.model_validate({
        "customer_id": "CUST-12345", "amount": amount,
        "reason": "Customer requested refund",
    })
    assert result.amount > 0


@pytest.mark.parametrize("amount", [0, -0.01, -1, True, float("nan"), float("inf"), -float("inf")])
def test_refund_rejects_invalid_amount(amount):
    with pytest.raises(ValidationError):
        RefundArgs.model_validate({
            "customer_id": "CUST-12345", "amount": amount,
            "reason": "Customer requested refund",
        })


@pytest.mark.parametrize("reason", ["", "short", "123456789", "          ", " x "])
def test_refund_rejects_short_or_blank_reason(reason):
    with pytest.raises(ValidationError):
        RefundArgs.model_validate({
            "customer_id": "CUST-12345", "amount": 1.0, "reason": reason,
        })


def test_refund_trims_reason_before_minimum_length_check():
    result = RefundArgs.model_validate({
        "customer_id": "CUST-12345", "amount": 1.0,
        "reason": "  valid reason  ",
    })
    assert result.reason == "valid reason"


def test_refund_rejects_extra_fields():
    with pytest.raises(ValidationError):
        RefundArgs.model_validate({
            "customer_id": "CUST-12345", "amount": 1.0,
            "reason": "Customer requested refund", "admin": True,
        })


def test_json_schemas_are_closed_objects():
    assert CUSTOMER_INPUT_SCHEMA["additionalProperties"] is False
    assert REFUND_INPUT_SCHEMA["additionalProperties"] is False
    assert set(REFUND_INPUT_SCHEMA["required"]) == {"customer_id", "amount", "reason"}
    assert REFUND_INPUT_SCHEMA["properties"]["amount"]["exclusiveMinimum"] == 0
