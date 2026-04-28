"""Historical bars endpoint.

GET /api/history?con_id=N — proxies to the engine's /engine/history.
GET /api/history?symbol=X&sec_type=STK — fallback for watchlist clicks
where we haven't qualified a contract yet.

The engine owns the IB connection and a tiny TTL cache for dedup. This
route is a thin pass-through; if the engine is down we return 503
rather than serving stale data.
"""
import os
import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/history", tags=["history"])


def _engine_url() -> str:
    port = os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    return f"http://127.0.0.1:{port}"


@router.get("")
async def get_history(
    con_id: int | None = None,
    symbol: str | None = None,
    sec_type: str = "STK",
    hours: int = 24,
    bar_size: str = "1 min",
):
    """Proxy to GET /engine/history. See engine endpoint for semantics."""
    if con_id is None and not symbol:
        raise HTTPException(status_code=400, detail="con_id or symbol is required")

    params: dict[str, str | int] = {"hours": hours, "bar_size": bar_size}
    if con_id is not None:
        params["con_id"] = int(con_id)
    if symbol:
        params["symbol"] = symbol
        params["sec_type"] = sec_type

    # IB's reqHistoricalDataAsync can take 10–25s for a cold contract
    # (qualify + 24h of 1-min bars, no RTH gate). Give the engine a
    # generous window before declaring unreachable.
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(f"{_engine_url()}/engine/history", params=params)
    except httpx.TimeoutException:
        logger.warning('{"event": "ENGINE_HISTORY_TIMEOUT", "params": %r}', params)
        return JSONResponse(
            content={"error": "Historical data fetch timed out — try again."},
            status_code=504,
        )
    except httpx.ConnectError:
        # Engine internal API on :8081 isn't bound yet (or has died).
        # Log at warning, not exception — this is a routine startup race
        # whenever the user clicks a chart row before the engine has
        # finished booting. Frontend will retry shortly.
        logger.warning(
            '{"event": "ENGINE_HISTORY_UNREACHABLE", "reason": "connect_refused"}',
        )
        return JSONResponse(
            content={"error": "Engine starting — try again in a moment."},
            status_code=503,
        )
    except Exception:
        logger.exception('{"event": "ENGINE_HISTORY_UNREACHABLE"}')
        return JSONResponse(content={"error": "Engine unavailable"}, status_code=503)

    if resp.status_code != 200:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    return JSONResponse(
        content=resp.json(),
        headers={"Cache-Control": "no-store, max-age=0"},
    )
