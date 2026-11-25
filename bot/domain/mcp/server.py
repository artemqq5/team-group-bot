import json
import logging
import os
import time
from collections import deque
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from bot.data.repositories.McpAuditRepository import McpAuditRepository
from bot.data.repositories.McpKeyRepository import McpKeyRepository
from bot.domain.mcp import tools

MCP_PATH = os.getenv("MCP_PATH", "/mcp")
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY = 64 * 1024
RATE_LIMIT = 60  # викликів на хвилину на ключ
SAFE_ARGS = ("chat_id", "since", "until", "query", "limit", "cursor")

router = APIRouter()

_rate: dict[int, deque] = {}
# (kind, key_id|ip, хвилина) -> id рядка аудиту (агреговані відмови)
_agg: dict[tuple, int] = {}


def _now() -> float:
    return time.time()


def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        last = xff.split(",")[-1].strip()
        if last:
            return last[:64]
    return request.client.host if request.client else None


def _rpc_error(req_id, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _sanitize(args) -> Optional[dict]:
    if not isinstance(args, dict):
        return None
    out = {}
    for k in SAFE_ARGS:
        if k in args:
            v = args[k]
            out[k] = v if isinstance(v, (int, float, bool)) or v is None else str(v)[:200]
    return out or None


async def _audit_aggregated(kind: str, ident, **fields) -> bool:
    """Один рядок на (kind, key_id|ip, хвилина) + hits. False — аудит недоступний (fail-closed)."""
    minute = int(_now() // 60)
    for k in [k for k in _agg if k[2] < minute - 1]:
        del _agg[k]
    key = (kind, ident, minute)
    existing = _agg.get(key)
    if existing is not None:
        await McpAuditRepository.increment_hits(existing)  # збій інкременту ігноруємо
        return True
    audit_id = await McpAuditRepository.add(**fields)
    if audit_id is None:
        return False
    _agg[key] = audit_id
    return True


def _rate_limited(key_id: int) -> bool:
    now = _now()
    q = _rate.setdefault(key_id, deque())
    while q and q[0] <= now - 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def _unauthorized(audit_ok: bool) -> Response:
    if not audit_ok:
        return JSONResponse({"error": "audit unavailable"}, status_code=503)
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


@router.api_route(MCP_PATH, methods=["GET", "DELETE"])
async def mcp_not_allowed():
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post(MCP_PATH)
async def mcp_endpoint(request: Request):
    if "origin" in request.headers:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        declared = int(request.headers.get("content-length", 0))
    except ValueError:
        declared = 0
    if declared > MAX_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    ip = _client_ip(request)

    # --- автентифікація ---
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        ok = await _audit_aggregated("no_token", ip, method="auth", status="denied", reason="no_token", client_ip=ip)
        return _unauthorized(ok)
    try:
        resolved = await McpKeyRepository.resolve_by_token(token)
    except Exception as e:
        logging.error(f"mcp: resolve failed: {type(e).__name__}")
        resolved = None
    if resolved is None:
        return JSONResponse({"error": "service unavailable"}, status_code=503)
    key, reason = resolved
    if reason is not None:
        ident = key.id if key is not None else ip
        ok = await _audit_aggregated(reason, ident, method="auth", status="denied", reason=reason,
                                     key_id=key.id if key is not None else None, client_ip=ip)
        return _unauthorized(ok)

    # --- розбір JSON-RPC ---
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        msg = None
        parse_failed = True
    else:
        parse_failed = False

    async def audit(method, status, **kw) -> bool:
        return await McpAuditRepository.add(method=method, status=status, key_id=key.id, client_ip=ip, **kw) is not None

    if parse_failed:
        await audit("-", "error", reason="bad_args")
        return _rpc_error(None, -32700, "parse error")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        await audit("-", "error", reason="bad_args")
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")

    req_id = msg.get("id")
    method = msg["method"][:64]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if _rate_limited(key.id):
        ok = await _audit_aggregated("rate_limited", key.id, method=method, status="denied",
                                     reason="rate_limited", key_id=key.id, client_ip=ip)
        if not ok:
            return _rpc_error(req_id, -32603, "audit unavailable")
        return _rpc_error(req_id, -32000, "rate limit exceeded")

    # --- обробка ---
    tool_name = None
    arguments = None
    status, a_reason, count = "ok", None, None
    try:
        if "id" not in msg:  # нотифікація
            response: Optional[dict] = None
        elif method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            response = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "mt-chats", "version": "1.0.0"}}
        elif method == "ping":
            response = {}
        elif method == "tools/list":
            response = {"tools": tools.TOOLS}
            count = len(tools.TOOLS)
        elif method == "tools/call":
            tool_name = str(params.get("name"))[:64]
            args = params.get("arguments") or {}
            arguments = _sanitize(args)
            handler = tools.HANDLERS.get(tool_name)
            if handler is None or not isinstance(args, dict):
                await audit(method, "error", tool_name=tool_name, arguments=arguments, reason="bad_args")
                return _rpc_error(req_id, -32602, "invalid params")
            try:
                payload, count = await handler(key.categories, args)
                response = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                            "isError": False}
            except tools.ToolError as te:
                status = "denied" if te.reason == "out_of_scope" else "error"
                a_reason = te.reason
                response = {"content": [{"type": "text", "text": te.message}], "isError": True}
        else:
            await audit(method, "error", reason="bad_args")
            return _rpc_error(req_id, -32601, "method not found")
    except Exception as e:
        logging.error(f"mcp: internal error: {type(e).__name__}")
        await audit(method, "error", tool_name=tool_name, arguments=arguments, reason="internal")
        return _rpc_error(req_id, -32603, "internal error")

    # fail-closed: без запису в аудит результат відкидаємо
    if not await audit(method, status, tool_name=tool_name, arguments=arguments, reason=a_reason,
                       result_count=count):
        return _rpc_error(req_id, -32603, "audit unavailable")
    await McpKeyRepository.touch(key.id)
    if response is None:
        return Response(status_code=202)
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": response})
