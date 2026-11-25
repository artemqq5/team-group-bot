# Plan: OAuth 2.1 for the MCP connector (claude.ai), no secret in URL

Owner (2026-11-25): remove Claude Code support entirely (no `claude mcp add`, no raw-key Bearer, no `/k/{token}`); key is pasted only on our sign-in page; connector URL has no secret.

## 1. What changes
- Remove `/k/{token}` routes, `_RedactKeyPath`, raw-key Bearer auth, `claude mcp add` text.
- Add built-in OAuth 2.1 AS under the `MCP_PATH` prefix: RFC 9728 + RFC 8414 discovery, DCR (RFC 7591), PKCE S256 authorize page (Russian) where the user pastes the bot key, token endpoint with opaque access/refresh tokens (sha256 stored only), refresh rotation + reuse detection.
- MCP accepts only OAuth access tokens; key resolved live via join to `mcp_keys` on every request (revoke/delete immediate; scope = key categories, live).
- Connector URL = `{WEBHOOK_BASE_URL}{MCP_PATH}`.

## 2. Files
- `bot/domain/mcp/server.py` — remove `/k/` routes, `_RedactKeyPath` + addFilter, `path_token` in `_handle`, `resolve_by_token` call; use `McpOAuthRepository.resolve_access`; new `WWW-Authenticate`.
- `bot/domain/mcp/oauth.py` (NEW) — APIRouter: well-known, register, authorize GET/POST, token; in-memory pending tx / codes / throttles; HTML.
- `bot/data/repositories/McpOAuthRepository.py` (NEW) — clients + tokens, static methods, `@log_exc_with_try_except()`.
- `bot/data/models.py` — `McpOAuthClientModel`, `McpOAuthTokenModel`.
- `alembic/versions/003_mcp_oauth.py` (NEW) — down_revision `3b7d9e4c1a52`.
- `bot/main.py` — include oauth router; `_retention_loop` deletes tokens with `expires_at < now-7d`.
- `bot/domain/routers/admin/chat/menu/mcp_keys_.py` — new secret message; connector URL line in key view.
- `McpKeyRepository.py` — no logic change; `resolve_by_token` used only by authorize POST.
- Tests: `tests/mcp_helpers.py` (`make_key` returns OAuth access token via `issue_pair`), `test_mcp_auth_audit.py` (drop `/k/` tests, header-only rewrites, 401 header asserts), `test_mcp_ui_handlers.py` (new text asserts), `tests/test_mcp_oauth.py` (NEW adversarial suite).
- No new env var, no new dependency (`python-multipart` absent → parse form via `urllib.parse.parse_qs` on capped body).

## 3. Contracts
Derived at request time (never from Host): `BASE = WEBHOOK_BASE_URL.rstrip("/")`, `R = BASE + MCP_PATH`, `P = MCP_PATH.rsplit("/",1)[0]` (prod `/team-group-bot`, tests `""`), `ISSUER = BASE + P`.

Tables:
- `mcp_oauth_clients`: `client_id` String(64) PK (`mcpc_`+token_urlsafe(24)), `client_name` String(255) null, `redirect_uris` JSON not null, `created_at` not null, `client_ip` String(64) null.
- `mcp_oauth_tokens`: `id` BigInteger PK, `key_id` int FK mcp_keys.id not null, `client_id` String(64), `family_id` String(32), `kind` String(8) access|refresh, `token_hash` String(64) UNIQUE, `created_at`, `expires_at` not null, `used_at` null, `revoked_at` null; indexes on family_id, key_id, expires_at.
- `mcp_audit_log` unchanged; new methods `oauth_register|oauth_authorize|oauth_token|oauth_refresh`; reasons `bad_code, code_replay, pkce_mismatch, redirect_mismatch, client_mismatch, refresh_reuse, throttled` + existing; `client_id` in `arguments`.

Endpoints (prod paths; tests `P=""`):
- GET `{P}/.well-known/oauth-protected-resource` (+ alias `{P}/.well-known/oauth-protected-resource/mcp`): `{"resource":R,"authorization_servers":[ISSUER],"bearer_methods_supported":["header"],"scopes_supported":["mcp"]}`.
- GET `{P}/.well-known/oauth-authorization-server` and `{P}/.well-known/openid-configuration` (same JSON): issuer, authorization_endpoint `ISSUER/oauth/authorize`, token_endpoint `ISSUER/oauth/token`, registration_endpoint `ISSUER/oauth/register`, response_types `["code"]`, grant_types `["authorization_code","refresh_token"]`, code_challenge_methods `["S256"]`, token_endpoint_auth_methods `["none"]`, scopes `["mcp"]`.
- MCP 401: `WWW-Authenticate: Bearer resource_metadata="<ISSUER>/.well-known/oauth-protected-resource"` (+ `, error="invalid_token"` when a token was presented). Body `{"error":"unauthorized"}`.
- POST `{P}/oauth/register` (JSON ≤8 KB): every redirect_uri must exactly equal `https://claude.ai/api/mcp/auth_callback` or `https://claude.com/api/mcp/auth_callback`, non-empty, else 400 `invalid_redirect_uri`; bad JSON 400 `invalid_client_metadata`; 201 with client_id, client_id_issued_at, client_name, redirect_uris, grant_types, response_types, `token_endpoint_auth_method:"none"`; 30/h per IP → 429.
- GET `{P}/oauth/authorize`: response_type=code, client_id, redirect_uri, code_challenge `^[A-Za-z0-9_-]{43}$`, method S256, state ≤2048, scope ignored, resource (if present) == R. Invalid → 400 HTML, NEVER redirect. redirect_uri in client's list AND allowlist. Success: `tx` in memory (params, IP, 10 min), cookie `tgb_oauth_tx` Path `{P}/oauth` Secure HttpOnly SameSite=Strict Max-Age 600; form with hidden `tx` + `key` (password, autocomplete off). Headers: no-store, X-Frame-Options DENY, Referrer-Policy no-referrer, CSP `default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://claude.ai https://claude.com; frame-ancestors 'none'`. Page shows host from validated redirect_uri, not client_name.
- POST `{P}/oauth/authorize` (form ≤4 KB), in order: IP throttle 10 failures/15 min → 429 (audit throttled, aggregated); Origin (if present) == origin of BASE; tx valid + cookie == tx (compare_digest) else "Сессия истекла…"; resolve key → fail: audit denied (key_id if known else aggregated), generic "Ключ не подошёл", never echo key, drop tx after 5 failures; success: audit ok (fail-closed: audit fails → error page, no code); code token_urlsafe(32) in memory {key_id, client_id, redirect_uri, challenge, exp +300 s, used}; pop tx; 303 to `redirect_uri?code&state`.
- POST `{P}/oauth/token` (form only ≤8 KB; no-store, Pragma no-cache; errors JSON 400 invalid_request|invalid_grant|unsupported_grant_type):
  - authorization_code: used code → revoke its family, audit code_replay, invalid_grant; mark used before validation; client_id ==, redirect_uri == exact, S256(verifier `^[A-Za-z0-9._~-]{43,128}$`) == challenge, resource == R if present, ≤5 min, key live → new family, access (1 h) + refresh pair, audit ok.
  - refresh_token: atomic `UPDATE … SET used_at=now WHERE id AND used_at IS NULL AND revoked_at IS NULL AND expires_at>now`; 0 rows with used/revoked → revoke family, audit refresh_reuse, invalid_grant; client_id == row; key live; revoke family's previous access tokens; new pair same family; audit oauth_refresh ok.
  - Success: `{"access_token":"tgba_…","token_type":"Bearer","expires_in":3600,"refresh_token":"tgbr_…","scope":"mcp"}`.
- `resolve_access(token)`: join token(kind=access, not revoked, not expired) + key(not revoked, not deleted, not expired) → `(key,None)` / `(key|None,reason)` / `None` on DB failure (503).

## 4. Order
1 models + revision → 2 repository → 3 oauth.py (helpers, reuse `server._client_ip`/`_audit_aggregated`; avoid circular import — `resource_metadata_url()` lives in oauth, server imports it) → 4 server.py → 5 main.py → 6 mcp_keys_.py text → 7 HTML → 8 tests (`make_key` via `issue_pair`; autouse reset of oauth in-memory stores; monkeypatch WEBHOOK_BASE_URL).

## 5. Tester (adversarial, `tests/test_mcp_oauth.py`)
PKCE wrong verifier burns code; plain/missing challenge → 400 page; code replay revokes first-exchange tokens; expired code; redirect mismatch; open-redirect variants (`https://evil`, `claude.ai.evil.com`, `/../x`, trailing `?`, uppercase host, other client's URI) → 400 no Location; client mismatch at token/refresh; unknown client at authorize; CSRF (no cookie, cookie≠tx, foreign Origin, foreign tx); wrong key generic error, body never echoes key, audited; 11th failure → 429 aggregated; revoked/deleted key cannot sign in; revoked/deleted mid-session → MCP 401 invalid_token + refresh invalid_grant; refresh reuse revokes family; header confusion (raw tgb_ key, refresh as Bearer, access as refresh_token, Basic, lowercase bearer valid, empty Bearer); token endpoint JSON body/missing grant/client_credentials/oversize; live scope; fail-closed audit; metadata with `MCP_PATH=/team-group-bot/mcp` (exact resource, prefixed endpoints, Host ignored); only hashes stored, no plaintext in DB/logs (caplog).

## 6. Ops risks
- nginx (outside repo) strict 10 r/m for requests without Authorization hits OAuth endpoints; Anthropic egress IPs shared → raise for `/team-group-bot/oauth/` and `/team-group-bot/.well-known/` before rollout.
- Discovery under prefix relies on `resource_metadata` header + `{issuer}/.well-known/openid-configuration`; fallback: nginx exact locations `/.well-known/oauth-authorization-server/team-group-bot` and `/.well-known/oauth-protected-resource/team-group-bot/mcp` → bot. Verify on real domain.
- Parallel refresh → reuse detection → reconnect (accepted; watch `refresh_reuse`).
- In-memory tx/codes: single uvicorn process; restart mid-sign-in → restart from Claude.
- Breaking: `/k/` connectors stop working; re-add with new URL, same key.

## Decided without asking
- DCR, not CIMD — no server-side fetch of client URLs (SSRF/latency); allowlist makes open DCR harmless.
- No python-multipart — parse_qs on capped body.
- Codes/pending/throttles in memory, tokens in DB.
- Revocation via live join, not token mass-update.
- Access TTL 1 h; refresh TTL **90 d** sliding (rotation) — matches what owner was told ("re-enter key after 90 days of non-use"); no absolute lifetime; key revoke is kill switch.
- No refresh grace window.
- Authorize errors render own 400 page, never redirect.
- Prefix/base from env, never Host header.
- Audit reuses `mcp_audit_log`.
- Expired tokens deleted after 7 d in daily retention loop.
- Sign-in page shows target host, not client_name.
- Scope `mcp` advertised/granted, requested scope ignored.
- Connector URL also shown in key view.
- No plan-reviewer round — owner asked to move fast; covered by adversarial tester pass.
