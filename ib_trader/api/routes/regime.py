"""Local-regime classifier endpoint.

GET /api/regime — proxies to the engine's /engine/regime. Returns
the same ADX/ATR/Donchian reading the chart_signal entry gate uses,
so the chart frontend can display it next to the price action.
"""
import os
import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/regime", tags=["regime"])


def _engine_url() -> str:
    port = os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    return f"http://127.0.0.1:{port}"


@router.get("")
async def get_regime(
    con_id: int | None = None,
    symbol: str | None = None,
    sec_type: str = "STK",
    hours: int = 24,
    bar_size: str = "3 mins",
    adx_period: int = 14,
    atr_period: int = 14,
    donchian_period: int = 20,
    trending_threshold: float = 25.0,
    ranging_threshold: float = 20.0,
):
    """Proxy to GET /engine/regime. See engine endpoint for semantics."""
    if con_id is None and not symbol:
        raise HTTPException(status_code=400, detail="con_id or symbol is required")

    params: dict[str, str | int | float] = {
        "hours": hours, "bar_size": bar_size,
        "adx_period": adx_period, "atr_period": atr_period,
        "donchian_period": donchian_period,
        "trending_threshold": trending_threshold,
        "ranging_threshold": ranging_threshold,
    }
    if con_id is not None:
        params["con_id"] = int(con_id)
    if symbol:
        params["symbol"] = symbol
        params["sec_type"] = sec_type

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(f"{_engine_url()}/engine/regime", params=params)
    except httpx.TimeoutException:
        logger.warning('{"event": "ENGINE_REGIME_TIMEOUT", "params": %r}', params)
        return JSONResponse(
            content={"error": "Regime fetch timed out — try again."},
            status_code=504,
        )
    except httpx.ConnectError:
        logger.warning('{"event": "ENGINE_REGIME_UNREACHABLE", "reason": "connect_refused"}')
        return JSONResponse(
            content={"error": "Engine starting — try again in a moment."},
            status_code=503,
        )
    except Exception:
        logger.exception('{"event": "ENGINE_REGIME_UNREACHABLE"}')
        return JSONResponse(content={"error": "Engine unavailable"}, status_code=503)

    if resp.status_code != 200:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    return JSONResponse(
        content=resp.json(),
        headers={"Cache-Control": "no-store, max-age=0"},
    )
