"""Positions endpoint.

GET /api/positions — returns current positions from the engine's positions
file (run/positions.json), which is refreshed every 10 seconds directly
from IB.  No SQLite involved — IB is the source of truth for positions.
"""
import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/positions", tags=["positions"])

_POSITIONS_FILE = Path("run/positions.json")


@router.get("")
def list_positions():
    """Return current broker positions from the engine's JSON cache."""
    try:
        data = json.loads(_POSITIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = []

    return JSONResponse(
        content=data,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )
