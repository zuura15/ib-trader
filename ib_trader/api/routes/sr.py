"""Canonical SR signal endpoint.

GET /api/sr — proxies to the engine's /engine/sr. Returns
lines + wedges + pivot timestamps so the chart frontend can render
exactly what the bot reasons about (single source of truth for SR
math).
"""
import os
import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sr", tags=["sr"])


def _engine_url() -> str:
    port = os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    return f"http://127.0.0.1:{port}"


@router.get("")
async def get_sr(
    con_id: int | None = None,
    symbol: str | None = None,
    sec_type: str = "STK",
    hours: int = 2,
    bar_size: str = "3 mins",
    near_touch_tolerance_fraction: float | None = None,
    break_stale_bars: int | None = None,
    include_broken_wedges: bool = False,
):
    """Proxy to GET /engine/sr. See engine endpoint for semantics."""
    if con_id is None and not symbol:
        raise HTTPException(status_code=400, detail="con_id or symbol is required")

    params: dict[str, str | int | float | bool] = {
        "hours": hours, "bar_size": bar_size,
        "include_broken_wedges": include_broken_wedges,
    }
    if con_id is not None:
        params["con_id"] = int(con_id)
    if symbol:
        params["symbol"] = symbol
        params["sec_type"] = sec_type
    if near_touch_tolerance_fraction is not None:
        params["near_touch_tolerance_fraction"] = near_touch_tolerance_fraction
    if break_stale_bars is not None:
        params["break_stale_bars"] = int(break_stale_bars)

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(f"{_engine_url()}/engine/sr", params=params)
    except httpx.TimeoutException:
        logger.warning('{"event": "ENGINE_SR_TIMEOUT", "params": %r}', params)
        return JSONResponse(
            content={"error": "SR fetch timed out — try again."},
            status_code=504,
        )
    except httpx.ConnectError:
        logger.warning('{"event": "ENGINE_SR_UNREACHABLE", "reason": "connect_refused"}')
        return JSONResponse(
            content={"error": "Engine starting — try again in a moment."},
            status_code=503,
        )
    except Exception:
        logger.exception('{"event": "ENGINE_SR_UNREACHABLE"}')
        return JSONResponse(content={"error": "Engine unavailable"}, status_code=503)

    if resp.status_code != 200:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    return JSONResponse(
        content=resp.json(),
        headers={"Cache-Control": "no-store, max-age=0"},
    )
