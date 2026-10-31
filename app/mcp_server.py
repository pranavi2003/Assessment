"""Strict customer MCP server using the official MCP SDK v2 low-level Server.

The explicit JSON Schemas set additionalProperties=false. Pydantic validates the
actual arguments again inside the handler. stdout is reserved for stdio JSON-RPC.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ValidationError

from app.mcp_schemas import (
    CustomerRecordArgs, RefundArgs, CUSTOMER_INPUT_SCHEMA, REFUND_INPUT_SCHEMA,
    CUSTOMER_OUTPUT_SCHEMA, REFUND_OUTPUT_SCHEMA,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("customer-mcp")


TOOLS = [
    Tool(
        name="get_customer_record",
        description="Retrieve a customer record using an ID formatted as CUST-XXXXX.",
        input_schema=CUSTOMER_INPUT_SCHEMA,
        output_schema=CUSTOMER_OUTPUT_SCHEMA,
    ),
    Tool(
        name="trigger_refund",
        description="Submit a refund request with a positive amount and reason of at least 10 characters.",
        input_schema=REFUND_INPUT_SCHEMA,
        output_schema=REFUND_OUTPUT_SCHEMA,
    ),
]


def validate(model: type[BaseModel], values: dict[str, Any]) -> BaseModel:
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        safe_errors = [
            {"field": ".".join(str(part) for part in item["loc"]),
             "type": item["type"]}
            for item in exc.errors()
        ]
        # MCPError is preserved as a JSON-RPC error by SDK v2's dispatcher.
        raise MCPError(code=-32602, message="Invalid tool arguments",
                       data={"validation_errors": safe_errors}) from exc


async def list_tools(
    _ctx: ServerRequestContext,
    _params: PaginatedRequestParams | None,
) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def call_tool(
    _ctx: ServerRequestContext,
    params: CallToolRequestParams,
) -> CallToolResult:
    arguments = params.arguments or {}

    if params.name == "get_customer_record":
        args = validate(CustomerRecordArgs, arguments)
        assert isinstance(args, CustomerRecordArgs)
        logger.info("Customer lookup requested")
        result = {
            "customer_id": args.customer_id,
            "name": "Alex Morgan",
            "status": "active",
            "tier": "gold",
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))],
            structured_content=result,
        )

    if params.name == "trigger_refund":
        args = validate(RefundArgs, arguments)
        assert isinstance(args, RefundArgs)
        logger.info("Refund request validated for customer %s", args.customer_id)
        # Deterministic assessment fixture; no payment is actually executed.
        result = {
            "accepted": True,
            "customer_id": args.customer_id,
            "amount": args.amount,
            "reason": args.reason,
            "refund_id": "REF-DEMO-0001",
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))],
            structured_content=result,
        )

    raise MCPError(code=-32602, message="Unknown tool")


server = Server(
    "customer-operations",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
