"""End-to-end stdio tests using the official MCP Python SDK v2 client."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from mcp import Client, MCPError, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


def server_params():
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "app" / "mcp_server.py")],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def test_stdio_tool_discovery_and_valid_lookup():
    async def run():
        async with Client(server_params()) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "get_customer_record", "trigger_refund"}
            result = await client.call_tool("get_customer_record", {
                "customer_id": "CUST-A1B2C"})
            assert not result.is_error
            assert result.structured_content["customer_id"] == "CUST-A1B2C"
    asyncio.run(run())


def test_invalid_customer_id_maps_to_jsonrpc_invalid_params():
    async def run():
        async with Client(server_params()) as client:
            with pytest.raises(MCPError) as error:
                await client.call_tool("get_customer_record", {"customer_id": "bad-id"})
            assert error.value.code == -32602
    asyncio.run(run())


def test_refund_validation_edge_cases_map_to_invalid_params():
    invalid_arguments = [
        {"customer_id": "CUST-12345", "amount": 0.0, "reason": "Customer request"},
        {"customer_id": "CUST-12345", "amount": -1.0, "reason": "Customer request"},
        {"customer_id": "CUST-12345", "amount": 1.0, "reason": "short"},
        {"customer_id": "BAD-ID", "amount": 1.0, "reason": "Customer request"},
        {"customer_id": "CUST-12345", "amount": 1.0, "reason": "          "},
    ]

    async def run():
        async with Client(server_params()) as client:
            for arguments in invalid_arguments:
                with pytest.raises(MCPError) as error:
                    await client.call_tool("trigger_refund", arguments)
                assert error.value.code == -32602
    asyncio.run(run())


def test_extra_refund_argument_is_rejected():
    async def run():
        async with Client(server_params()) as client:
            with pytest.raises(MCPError) as error:
                await client.call_tool("trigger_refund", {
                    "customer_id": "CUST-12345", "amount": 10.0,
                    "reason": "Customer requested refund", "unexpected": "value"})
            assert error.value.code == -32602
    asyncio.run(run())
