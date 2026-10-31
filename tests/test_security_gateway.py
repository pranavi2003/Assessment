import json


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method,
            **({"params": params} if params is not None else {})}


def test_missing_auth_is_rejected(gateway_client):
    response = gateway_client.post("/mcp", json=rpc("tools/list"))
    assert response.status_code == 401


def test_tools_list_forwards_transparently(gateway_client):
    payload = rpc("tools/list")
    response = gateway_client.post("/mcp", json=payload,
                                   headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 200
    assert response.json()["id"] == 1
    assert response.json()["result"]["tools"][0]["name"] == "get_customer_record"


def test_viewer_cannot_call_admin_tool_and_downstream_is_not_invoked(gateway_client):
    response = gateway_client.post(
        "/mcp", json=rpc("tools/call", {"name": "admin_reset_key"}),
        headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == -32001
    assert response.json()["error"]["message"] == "Unauthorized Tool Call"


def test_admin_can_call_admin_tool(gateway_client):
    response = gateway_client.post(
        "/mcp", json=rpc("tools/call", {"name": "admin_reset_key"}),
        headers={"Authorization": "Bearer admin-token"})
    assert response.status_code == 200
    assert response.json()["result"]["content"]


def test_invalid_json_returns_parse_error(gateway_client):
    response = gateway_client.post("/mcp", content="{", headers={
        "Authorization": "Bearer viewer-token", "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_invalid_jsonrpc_version_rejected(gateway_client):
    response = gateway_client.post("/mcp", json={"jsonrpc": "1.0", "id": 1,
        "method": "tools/list"}, headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


def test_unknown_method_rejected(gateway_client):
    response = gateway_client.post("/mcp", json=rpc("admin/unknown"),
                                   headers={"Authorization": "Bearer admin-token"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601


def test_tools_call_requires_name(gateway_client):
    response = gateway_client.post("/mcp", json=rpc("tools/call", {}),
                                   headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


def test_jsonrpc_notification_gets_no_response_body(gateway_client):
    payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}}
    response = gateway_client.post("/mcp", json=payload,
                                   headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 202
    assert response.content == b""


def test_invalid_boolean_request_id_is_sanitized_to_null(gateway_client):
    response = gateway_client.post("/mcp", json={"jsonrpc": "2.0", "id": True,
        "method": "tools/list"}, headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 400
    assert response.json()["id"] is None


def test_nonstandard_json_nan_is_rejected(gateway_client):
    response = gateway_client.post("/mcp", content='{"jsonrpc":"2.0","id":NaN,"method":"tools/list"}',
        headers={"Authorization": "Bearer viewer-token", "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_mcp_tools_list_rejects_array_params(gateway_client):
    response = gateway_client.post("/mcp", json=rpc("tools/list", []),
                                   headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602
