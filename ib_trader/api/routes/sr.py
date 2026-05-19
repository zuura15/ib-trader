"""Canonical SR signal endpoint.

GET /api/sr        — proxies to the engine's /engine/sr (strict 3-touch fan).
GET /api/sr/fuzzy  — Layer-2 detector: RANSAC fuzzy lines + scored pivots +
                     parallel channels. Computed in the API process
                     (no engine round-trip on the detection step;
                     bars are still fetched via /engine/history).
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


@router.get("/fuzzy")
async def get_sr_fuzzy(
    con_id: int | None = None,
    symbol: str | None = None,
    sec_type: str = "STK",
    hours: int = 8,
    bar_size: str = "3 mins",
    prominence_fraction: float | None = None,
    residual_fraction: float | None = None,
    min_inliers: int | None = None,
    window_bars: int | None = None,
    curve_degree: int | None = None,
    curve_window_bars: int | None = None,
):
    """Layer-2 fuzzy SR detection.

    Pulls bars from /engine/history then runs
    ``ib_trader.signals.fuzzy_lines.detect_fuzzy`` locally. Response
    shape mirrors the existing /api/sr shape so the chart can render
    both overlays with similar code paths:

      {
        "bars_count": int,
        "pivots":   [{ts, idx, price, kind, prominence, rank, width}],
        "lines":    [{type, slope, intercept, from_ts, to_ts, from_idx,
                      to_idx, from_price, to_price, inlier_count,
                      inlier_idxs, score, age_bars}],
        "channels": [{support: <line>, resistance: <line>,
                      width_at_mid, slope_diff, span_bars, score}],
        "config":   {...the tunables that ran...}
      }
    """
    if con_id is None and not symbol:
        raise HTTPException(status_code=400, detail="con_id or symbol is required")

    # Hit the engine's /engine/history endpoint for bars.
    hist_params: dict[str, str | int | float | bool] = {
        "hours": hours, "bar_size": bar_size, "include_partial": True,
    }
    if con_id is not None:
        hist_params["con_id"] = int(con_id)
    if symbol:
        hist_params["symbol"] = symbol
        hist_params["sec_type"] = sec_type
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(f"{_engine_url()}/engine/history", params=hist_params)
    except httpx.TimeoutException:
        logger.warning('{"event": "ENGINE_HISTORY_TIMEOUT_FUZZY"}')
        return JSONResponse(content={"error": "history fetch timed out"}, status_code=504)
    except httpx.ConnectError:
        return JSONResponse(content={"error": "Engine starting"}, status_code=503)
    except Exception:
        logger.exception('{"event": "ENGINE_HISTORY_UNREACHABLE_FUZZY"}')
        return JSONResponse(content={"error": "Engine unavailable"}, status_code=503)
    if resp.status_code != 200:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    bars = resp.json() or []
    if not isinstance(bars, list) or len(bars) < 10:
        return JSONResponse(content={
            "bars_count": len(bars or []),
            "pivots": [], "lines": [], "channels": [],
            "config": {},
            "warning": "insufficient bars for fuzzy detection",
        })

    closes = [float(b["close"]) for b in bars]
    timestamps = [str(b["ts"]) for b in bars]

    # Build detection kwargs only when the caller actually overrode a
    # default — keeps the endpoint hermetic to library defaults.
    from ib_trader.signals.fuzzy_lines import detect_fuzzy
    kwargs: dict = {}
    if prominence_fraction is not None:
        kwargs["prominence_fraction"] = prominence_fraction
    if residual_fraction is not None:
        kwargs["residual_fraction"] = residual_fraction
    if min_inliers is not None:
        kwargs["min_inliers"] = int(min_inliers)
    if window_bars is not None:
        kwargs["window_bars"] = int(window_bars)
    if curve_degree is not None:
        kwargs["curve_degree"] = int(curve_degree)
    if curve_window_bars is not None:
        kwargs["curve_window_bars"] = int(curve_window_bars)
    det = detect_fuzzy(closes, **kwargs)

    def _line_json(L) -> dict:
        from_idx = max(0, min(L.from_idx, len(timestamps) - 1))
        to_idx = max(0, min(L.to_idx, len(timestamps) - 1))
        return {
            "type": L.type,
            "slope": L.slope,
            "intercept": L.intercept,
            "from_idx": L.from_idx,
            "to_idx": L.to_idx,
            "from_ts": timestamps[from_idx],
            "to_ts": timestamps[to_idx],
            "from_price": L.value_at(L.from_idx),
            "to_price": L.value_at(L.to_idx),
            "inlier_count": L.inlier_count,
            "inlier_idxs": L.inlier_idxs,
            "score": L.score,
            "age_bars": L.age_bars,
            "residual_threshold": L.residual_threshold,
        }

    return JSONResponse(
        content={
            "bars_count": len(bars),
            "pivots": [
                {
                    "idx": p.idx,
                    "ts": timestamps[max(0, min(p.idx, len(timestamps) - 1))],
                    "price": p.price,
                    "kind": p.kind,
                    "prominence": p.prominence,
                    "width": p.width,
                    "rank": p.rank,
                }
                for p in det.pivots
            ],
            "lines": [_line_json(L) for L in det.lines],
            "channels": [
                {
                    "support": _line_json(c.support),
                    "resistance": _line_json(c.resistance),
                    "width_at_mid": c.width_at_mid,
                    "slope_diff": c.slope_diff,
                    "span_bars": c.span_bars,
                    "score": c.score,
                }
                for c in det.channels
            ],
            "curve": (
                {
                    "degree": det.curve.degree,
                    "window_bars": det.curve.window_bars,
                    "start_idx": det.curve.start_idx,
                    "end_idx": det.curve.end_idx,
                    # ts for each fitted point so the frontend can plot
                    # the curve without knowing the bar-index space.
                    "points": [
                        {"ts": timestamps[i], "value": v}
                        for i, v in zip(
                            range(det.curve.start_idx, det.curve.end_idx + 1),
                            det.curve.values,
                        )
                        if 0 <= i < len(timestamps)
                    ],
                    "r_squared": det.curve.r_squared,
                    "coeffs": det.curve.coeffs,
                }
                if det.curve is not None else None
            ),
            "config": det.config,
        },
        headers={"Cache-Control": "no-store, max-age=0"},
    )
