"""Вбудований OAuth 2.1 сервер для MCP-конектора claude.ai (DCR + PKCE S256, opaque-токени)."""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
from collections import deque
from html import escape
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from bot.data.repositories.McpAuditRepository import McpAuditRepository
from bot.data.repositories.McpKeyRepository import McpKeyRepository
from bot.data.repositories.McpOAuthRepository import McpOAuthRepository
from bot.domain.mcp import server

# server імпортує oauth лише всередині функції — циклу при імпорті немає
router = APIRouter()

ALLOWED_REDIRECTS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
COOKIE = "tgb_oauth_tx"
TX_TTL = 600
CODE_TTL = 300
MAX_TX_FAILS = 5
IP_FAIL_LIMIT = 10
IP_FAIL_WINDOW = 15 * 60
REGISTER_LIMIT = 30
REGISTER_WINDOW = 3600
MAX_STORE = 10000

_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")

# Усе нижче — у пам'яті одного процесу (uvicorn без воркерів)
_tx: dict[str, dict] = {}
_codes: dict[str, dict] = {}
_fail_ip: dict[str, deque] = {}
_reg_ip: dict[str, deque] = {}


def _reset() -> None:
    _tx.clear()
    _codes.clear()
    _fail_ip.clear()
    _reg_ip.clear()


# ─── Похідні адреси (з env, ніколи з Host) ────────────────────────────────────

def _base() -> str:
    return os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")


def _prefix() -> str:
    return server.MCP_PATH.rsplit("/", 1)[0]


def _issuer() -> str:
    return _base() + _prefix()


def _resource() -> str:
    return _base() + server.MCP_PATH


def _origin_of(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def resource_metadata_url() -> str:
    return f"{_issuer()}/.well-known/oauth-protected-resource"


PREFIX = server.MCP_PATH.rsplit("/", 1)[0]
_PRM_ALIAS = "/" + server.MCP_PATH.rsplit("/", 1)[1]


# ─── Допоміжне ────────────────────────────────────────────────────────────────

def _prune_store(store: dict, now: float) -> None:
    for k in [k for k, v in store.items() if v["exp"] <= now]:
        del store[k]
    while len(store) >= MAX_STORE:  # верхня межа пам'яті: витісняємо найстаріші
        del store[next(iter(store))]


def _window(store: dict, ident: str, window: int, now: float) -> deque:
    if len(store) > MAX_STORE:
        for k in [k for k, q in store.items() if not q or q[-1] <= now - window]:
            del store[k]
    q = store.setdefault(ident, deque())
    while q and q[0] <= now - window:
        q.popleft()
    return q


def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _err(code: str, status: int = 400) -> JSONResponse:
    return _json({"error": code}, status)


def _page(title: str, body: str, status: int = 200, cookie_tx: Optional[str] = None) -> HTMLResponse:
    html = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:sans-serif;max-width:420px;margin:12vh auto;padding:0 16px}"
        "input{width:100%;box-sizing:border-box;padding:8px;margin:8px 0}"
        "button{padding:8px 16px}.err{color:#b00020}</style></head>"
        f"<body>{body}</body></html>"
    )
    resp = HTMLResponse(html, status_code=status, headers={
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                    "form-action 'self' https://claude.ai https://claude.com; frame-ancestors 'none'"),
    })
    if cookie_tx:
        resp.set_cookie(COOKIE, cookie_tx, max_age=TX_TTL, path=f"{PREFIX}/oauth", secure=True, httponly=True,
                        samesite="strict")
    return resp


def _bad_request_page() -> HTMLResponse:
    return _page("Ошибка", "<h2>Некорректный запрос</h2><p>Вернитесь в Claude и начните подключение заново.</p>", 400)


def _form_page(tx: str, host: str, error: Optional[str] = None, status: int = 200,
               cookie_tx: Optional[str] = None) -> HTMLResponse:
    err = f'<p class="err">{escape(error)}</p>' if error else ""
    body = (
        "<h2>Подключение к MT Messaging Bot</h2>"
        f"<p>Приложение <b>{escape(host)}</b> запрашивает доступ к архиву чатов (только чтение).</p>"
        "<p>Введите ключ MCP, выданный в боте.</p>"
        f"{err}"
        f'<form method="post" action="{PREFIX}/oauth/authorize">'
        f'<input type="hidden" name="tx" value="{escape(tx)}">'
        '<input type="password" name="key" autocomplete="off" autocapitalize="off" spellcheck="false" '
        'placeholder="tgb_…" required>'
        '<button type="submit">Подключить</button></form>'
    )
    return _page("Вход", body, status, cookie_tx)


async def _read_body(request: Request, limit: int) -> Optional[bytes]:
    try:
        declared = int(request.headers.get("content-length", 0))
    except ValueError:
        declared = 0
    if declared > limit:
        return None
    body = await request.body()
    return None if len(body) > limit else body


def _parse_form(body: bytes) -> Optional[dict]:
    """Повторений параметр = помилка (неоднозначність)."""
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=bool(body))
    except (UnicodeDecodeError, ValueError):
        return None
    if any(len(v) != 1 for v in parsed.values()):
        return None
    return {k: v[0] for k, v in parsed.items()}


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


# ─── Discovery ────────────────────────────────────────────────────────────────

@router.get(PREFIX + "/.well-known/oauth-protected-resource")
@router.get(PREFIX + "/.well-known/oauth-protected-resource" + _PRM_ALIAS)
async def protected_resource():
    return JSONResponse({
        "resource": _resource(),
        "authorization_servers": [_issuer()],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


@router.get(PREFIX + "/.well-known/oauth-authorization-server")
@router.get(PREFIX + "/.well-known/openid-configuration")
async def authorization_server():
    iss = _issuer()
    return JSONResponse({
        "issuer": iss,
        "authorization_endpoint": iss + "/oauth/authorize",
        "token_endpoint": iss + "/oauth/token",
        "registration_endpoint": iss + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


# ─── Dynamic Client Registration ──────────────────────────────────────────────

@router.post(PREFIX + "/oauth/register")
async def register(request: Request):
    ip = server._client_ip(request)
    body = await _read_body(request, 8 * 1024)
    if body is None:
        return _err("invalid_client_metadata")
    now = server._now()
    q = _window(_reg_ip, ip or "-", REGISTER_WINDOW, now)
    if len(q) >= REGISTER_LIMIT:
        ok = await server._audit_aggregated("throttled", ("reg", ip), method="oauth_register", status="denied",
                                            reason="throttled", client_ip=ip)
        return _err("temporarily_unavailable" if ok else "server_error", 429 if ok else 503)
    q.append(now)
    try:
        meta = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _err("invalid_client_metadata")
    if not isinstance(meta, dict):
        return _err("invalid_client_metadata")
    uris = meta.get("redirect_uris")
    name = meta.get("client_name")
    if name is not None and not isinstance(name, str):
        return _err("invalid_client_metadata")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris) \
            or any(u not in ALLOWED_REDIRECTS for u in uris):
        return _err("invalid_redirect_uri")
    uris = list(dict.fromkeys(uris))
    client_id = await McpOAuthRepository.create_client(name, uris, ip)
    if client_id is None:
        return _err("server_error", 503)
    if await McpAuditRepository.add(method="oauth_register", status="ok", arguments={"client_id": client_id},
                                    client_ip=ip) is None:
        return _err("server_error", 503)
    return _json({
        "client_id": client_id,
        "client_id_issued_at": int(now),
        "client_name": name[:255] if name else None,
        "redirect_uris": uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, 201)


# ─── Authorize ────────────────────────────────────────────────────────────────

@router.get(PREFIX + "/oauth/authorize")
async def authorize_get(request: Request):
    qp = request.query_params
    client_id = qp.get("client_id", "")
    redirect_uri = qp.get("redirect_uri", "")
    challenge = qp.get("code_challenge", "")
    state = qp.get("state", "")
    resource = qp.get("resource")
    if (qp.get("response_type") != "code" or qp.get("code_challenge_method") != "S256"
            or not _CHALLENGE_RE.match(challenge) or len(state) > 2048
            or (resource is not None and resource != _resource())
            or redirect_uri not in ALLOWED_REDIRECTS or not client_id):
        return _bad_request_page()
    client = await McpOAuthRepository.get_client(client_id)
    if client is None or redirect_uri not in (client.redirect_uris or []):
        return _bad_request_page()
    now = server._now()
    _prune_store(_tx, now)
    tx = secrets.token_urlsafe(32)
    _tx[tx] = {"client_id": client_id, "redirect_uri": redirect_uri, "challenge": challenge, "state": state,
               "has_state": "state" in qp, "ip": server._client_ip(request), "exp": now + TX_TTL, "fails": 0}
    return _form_page(tx, urlsplit(redirect_uri).hostname or "", cookie_tx=tx)


@router.post(PREFIX + "/oauth/authorize")
async def authorize_post(request: Request):
    ip = server._client_ip(request)
    now = server._now()
    fails = _window(_fail_ip, ip or "-", IP_FAIL_WINDOW, now)
    if len(fails) >= IP_FAIL_LIMIT:
        ok = await server._audit_aggregated("throttled", ("auth", ip), method="oauth_authorize", status="denied",
                                            reason="throttled", client_ip=ip)
        if not ok:
            return _page("Ошибка", "<p>Сервис временно недоступен.</p>", 503)
        return _page("Ошибка", "<p>Слишком много попыток. Повторите позже.</p>", 429)
    body = await _read_body(request, 4 * 1024)
    if body is None:
        return _page("Ошибка", "<p>Слишком большой запрос.</p>", 413)
    origin = request.headers.get("origin")
    if origin is not None and origin != _origin_of(_base()):
        return _page("Ошибка", "<p>Запрещено.</p>", 403)
    form = _parse_form(body)
    tx_id = form.get("tx", "") if form else ""
    cookie = request.cookies.get(COOKIE)
    tx = _tx.get(tx_id)
    if (form is None or tx is None or tx["exp"] <= now or cookie is None
            or not secrets.compare_digest(cookie.encode(), tx_id.encode())):
        return _page("Сессия истекла",
                     "<p>Сессия истекла. Вернитесь в Claude и начните подключение заново.</p>", 400)
    host = urlsplit(tx["redirect_uri"]).hostname or ""

    key_text = form.get("key", "")
    resolved = await McpKeyRepository.resolve_by_token(key_text) if 0 < len(key_text) <= 200 else (None, "invalid_token")
    if resolved is None:
        return _page("Ошибка", "<p>Сервис временно недоступен.</p>", 503)
    key, reason = resolved
    args = {"client_id": tx["client_id"][:64]}
    if reason is not None:
        fails.append(now)
        tx["fails"] += 1
        ok = await server._audit_aggregated(reason, key.id if key is not None else ip, method="oauth_authorize",
                                            status="denied", reason=reason, key_id=key.id if key is not None else None,
                                            arguments=args, client_ip=ip)
        if not ok:
            return _page("Ошибка", "<p>Сервис временно недоступен.</p>", 503)
        if tx["fails"] >= MAX_TX_FAILS:
            _tx.pop(tx_id, None)
            return _page("Сессия истекла",
                         "<p>Сессия истекла. Вернитесь в Claude и начните подключение заново.</p>", 400)
        return _form_page(tx_id, host, "Ключ не подошёл", 401)

    # fail-closed: без запису в аудит код не видається
    if await McpAuditRepository.add(method="oauth_authorize", status="ok", key_id=key.id, arguments=args,
                                    client_ip=ip) is None:
        return _page("Ошибка", "<p>Сервис временно недоступен.</p>", 503)
    _prune_store(_codes, now)
    code = secrets.token_urlsafe(32)
    _codes[code] = {"key_id": key.id, "client_id": tx["client_id"], "redirect_uri": tx["redirect_uri"],
                    "challenge": tx["challenge"], "exp": now + CODE_TTL, "used": False, "family": None}
    _tx.pop(tx_id, None)
    params = {"code": code}
    if tx["has_state"]:
        params["state"] = tx["state"]
    resp = Response(status_code=303, headers={"Location": f"{tx['redirect_uri']}?{urlencode(params)}",
                                              "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    resp.delete_cookie(COOKIE, path=f"{PREFIX}/oauth")
    return resp


# ─── Token ────────────────────────────────────────────────────────────────────

async def _deny(reason: str, ip, client_id: str, key_id: Optional[int] = None, error: str = "invalid_grant",
                method: str = "oauth_token") -> JSONResponse:
    await server._audit_aggregated(reason, key_id if key_id is not None else ip, method=method, status="denied",
                                   reason=reason, key_id=key_id, arguments={"client_id": client_id[:64]},
                                   client_ip=ip)
    return _err(error)


async def _success(access: str, refresh: str, key, family: str, client_id: str, ip, method: str):
    # fail-closed: аудит не записався — токени не віддаємо
    if await McpAuditRepository.add(method=method, status="ok", key_id=key.id, arguments={"client_id": client_id[:64]},
                                    client_ip=ip) is None:
        await McpOAuthRepository.revoke_family(family)
        return _err("server_error", 503)
    return _json({"access_token": access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh,
                  "scope": "mcp"})


@router.post(PREFIX + "/oauth/token")
async def token(request: Request):
    ip = server._client_ip(request)
    body = await _read_body(request, 8 * 1024)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if body is None or ctype != "application/x-www-form-urlencoded":
        return _err("invalid_request")
    form = _parse_form(body)
    if form is None:
        return _err("invalid_request")
    grant = form.get("grant_type")
    client_id = form.get("client_id", "")
    if not grant or not client_id:
        return _err("invalid_request")

    if grant == "authorization_code":
        code, verifier, redirect_uri = form.get("code"), form.get("code_verifier"), form.get("redirect_uri")
        if not code or not verifier or not redirect_uri:
            return _err("invalid_request")
        rec = _codes.get(code)
        if rec is None:
            return await _deny("bad_code", ip, client_id)
        if rec["used"]:
            if rec["family"]:
                await McpOAuthRepository.revoke_family(rec["family"])
            return await _deny("code_replay", ip, client_id, rec["key_id"])
        rec["used"] = True  # спалюємо код до будь-якої перевірки
        if rec["exp"] <= server._now():
            return await _deny("bad_code", ip, client_id, rec["key_id"])
        if client_id != rec["client_id"]:
            return await _deny("client_mismatch", ip, client_id, rec["key_id"])
        if redirect_uri != rec["redirect_uri"]:
            return await _deny("redirect_mismatch", ip, client_id, rec["key_id"])
        if not _VERIFIER_RE.match(verifier) or not secrets.compare_digest(_s256(verifier), rec["challenge"]):
            return await _deny("pkce_mismatch", ip, client_id, rec["key_id"])
        resource = form.get("resource")
        if resource is not None and resource != _resource():
            return await _deny("bad_code", ip, client_id, rec["key_id"])
        key = await McpOAuthRepository.get_live_key(rec["key_id"])
        if key is None:
            return await _deny("revoked", ip, client_id, rec["key_id"])
        pair = await McpOAuthRepository.issue_pair(key.id, client_id)
        if pair is None:
            return _err("server_error", 503)
        access, refresh, family = pair
        rec["family"] = family
        return await _success(access, refresh, key, family, client_id, ip, "oauth_token")

    if grant == "refresh_token":
        refresh_token = form.get("refresh_token")
        if not refresh_token:
            return _err("invalid_request")
        res = await McpOAuthRepository.refresh(refresh_token, client_id)
        if res is None:
            return _err("server_error", 503)
        status, data = res
        if status == "reuse":
            family, key_id = data
            await McpOAuthRepository.revoke_family(family)
            return await _deny("refresh_reuse", ip, client_id, key_id, method="oauth_refresh")
        if status == "client_mismatch":
            return await _deny("client_mismatch", ip, client_id, method="oauth_refresh")
        if status != "ok":
            return await _deny("invalid_token", ip, client_id, method="oauth_refresh")
        access, refresh, key, family = data
        return await _success(access, refresh, key, family, client_id, ip, "oauth_refresh")

    return _err("unsupported_grant_type")
