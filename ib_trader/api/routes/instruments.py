"""Instrument discovery endpoints.

GET /api/instruments/expiries — list upcoming futures expiries for a root.

The engine process holds the IB connection, so this route proxies to the
engine's internal API on ``127.0.0.1:{engine_internal_port}``. The
engine's ``/engine/instruments/expiries`` handler does the actual IB
``reqContractDetails`` call and filters expired contracts.
"""
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/instruments", tags=["instruments"])


def _engine_url() -> str:
    port = os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    return f"http://127.0.0.1:{port}"


@router.get("/expiries")
async def list_expiries(
    root: str = Query(..., description="Futures root (ES, MES, NQ...)"),
    exchange: str = Query("CME", description="Exchange code (CME, NYMEX...)"),
    trading_class: str | None = Query(None, description="Optional trading class disambiguator"),
):
    """Proxy to the engine's in-process IB discovery call."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_engine_url()}/engine/instruments/expiries",
                params={
                    "root": root,
                    "exchange": exchange,
                    **({"trading_class": trading_class} if trading_class else {}),
                },
            )
    except httpx.ConnectError:
        logger.exception('{"event": "ENGINE_UNREACHABLE"}')
        return JSONResponse(content={"error": "Engine unavailable"}, status_code=503)

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()
