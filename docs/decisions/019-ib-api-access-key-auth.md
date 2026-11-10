# ADR-019: ib-api Requires an Access Key (Bearer for clients, HttpOnly Cookie for Browsers)

**Date:** 2026-11-10
**Status:** Accepted

## Context

ib-api binds `0.0.0.0:8000`, serves the SPA, and can place orders (`/api/commands`,
`/api/orders/cancel`, bot start/force-sell). `APIKeyMiddleware` existed but
`ib-api`'s `main()` never passed `api_key` to `create_app`, so anyone who could
reach :8000 on the LAN or tailnet could trade. The middleware was also a
`BaseHTTPMiddleware`, which never sees WebSocket scopes, so `/ws` had no check even when a key was set.

The Burrow Android app (zuura15/burrow, issue #104) reaches ib-api over an
embedded Tailscale node, and the tailnet ACL alone shouldn't be the only control.

## Decision

- `API_SECRET_KEY` in `.env` is mandatory: at least 32 characters. `ib-api` exits with code 2 if it is missing or too short.
- Pure-ASGI middleware guards `/api/*` (except `/api/auth/{login,logout,session}`),
  `/ws`, and the OpenAPI docs, for both HTTP and WebSocket scopes.
- Accepted credentials:
  - `Authorization: Bearer <key>`: scripts (`scripts/api_curl.sh`), Playwright,
    and Burrow (its proxy injects the header, including on the `/ws` upgrade).
  - An `ibt_session` cookie set by `POST /api/auth/login`: browsers. The cookie is HttpOnly and
    `SameSite=Strict`, and its value is HMAC-SHA256(key, fixed context), so the key
    itself never reaches browser storage. Rotating the key invalidates every session.
- `?token=` query parameters are **not** accepted. Tokens in URLs leak into logs
  and history.
- The SPA shell and its static assets stay public so a browser can render the
  login screen. The bundle holds no secrets.
- `create_app(api_key=None)` still disables auth, but only tests use it.

## Consequences

- Every script that calls :8000 goes through `scripts/api_curl.sh`, which hands the key to
  curl on stdin so it stays out of `ps`. `check_dev_can_shutdown.sh` treats a
  rejected key as "unsafe to shut down" and no longer fails open.
- `deploy_prod.sh` refuses to deploy if prod's `.env` has no key.
- Browsers sign in once per 30 days (`api_auth_cookie_days`). WebSocket and SSE requests
  carry the cookie automatically, so the frontend has no per-call auth code.
- CSRF and cross-site WebSocket hijacking are mitigated by `SameSite=Strict`. There
  is no separate Origin check, because Vite on LAN IPs would trip it.
