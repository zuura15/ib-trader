"""Gateway-mode auto-detection.

Probes a candidate list of (port, label) endpoints, connects with a bare
ib_async IB() session just long enough to read managedAccounts, and returns
the winning port plus the discovered account list. Callers use the account-id
prefix (``DU*`` = paper, everything else = live) to pick the right account_id
and market-data type from .env.

Lives inside ``ib_trader.ib.*`` because it imports ib_async directly: the
import-linter contract for ``ib_async`` access whitelists this package.
The probe is short-lived and must not leave event handlers registered on
the surviving client — that's why this is a sibling of ``insync_client``
rather than a method on it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ib_async import IB

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    port: int
    label: str  # e.g. "gateway-live", "gateway-paper", "tws-live"


# Default probe order: live Gateway first, paper Gateway second, then TWS.
# Rationale: the user normally runs one Gateway at a time; probing live first
# means we only land on paper when live is not up. Callers can override via
# settings.yaml `ib_port_candidates`.
DEFAULT_CANDIDATES: tuple[Candidate, ...] = (
    Candidate(4001, "gateway-live"),
    Candidate(4002, "gateway-paper"),
    Candidate(7496, "tws-live"),
    Candidate(7497, "tws-paper"),
)


@dataclass(frozen=True)
class ProbeResult:
    port: int
    label: str
    accounts: list[str]
    mode: str  # "paper" | "live"


def _classify(accounts: list[str]) -> str:
    """Derive mode from the account-id prefix.

    IB paper accounts always begin with ``DU``; everything else is live.
    If *any* account on the session is paper we treat the session as paper —
    mixing paper and live accounts on one Gateway session is not something
    IB supports in practice, but the conservative read-as-paper choice means
    the operator gets the paper market-data type (delayed) instead of
    accidentally requesting live data on a paper session.
    """
    if not accounts:
        raise RuntimeError("Gateway reported no managed accounts")
    if any(a.startswith("DU") for a in accounts):
        return "paper"
    return "live"


async def probe_gateway(
    host: str,
    candidates: list[Candidate],
    client_id: int,
    timeout: float = 2.0,
) -> ProbeResult:
    """Connect to each candidate port in order; return the first success.

    Uses a bare ib_async IB() session for each probe so event handlers are
    not left registered on the live client. Disconnects after reading
    managedAccounts.

    Raises RuntimeError if every candidate fails.
    """
    errors: list[str] = []
    for cand in candidates:
        ib = IB()
        try:
            await ib.connectAsync(host, cand.port, clientId=client_id, timeout=timeout)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            errors.append(f"{host}:{cand.port} ({cand.label}) — {e}")
            logger.debug(
                '{"event": "GATEWAY_PROBE_MISS", "port": %d, "label": "%s", "error": "%s"}',
                cand.port, cand.label, str(e),
            )
            continue

        try:
            accounts = list(ib.managedAccounts())
            mode = _classify(accounts)
            logger.info(
                '{"event": "GATEWAY_PROBE_HIT", "port": %d, "label": "%s", '
                '"mode": "%s", "accounts": %d}',
                cand.port, cand.label, mode, len(accounts),
            )
            return ProbeResult(port=cand.port, label=cand.label, accounts=accounts, mode=mode)
        finally:
            try:
                ib.disconnect()
            except Exception as e:
                logger.debug("probe disconnect failed: %s", e)

    raise RuntimeError(
        "No IB Gateway or TWS instance reachable. Tried: " + "; ".join(errors)
    )


def load_candidates(settings: dict) -> list[Candidate]:
    """Build the candidate list from settings.

    `ib_port_candidates` in settings.yaml may be either:
    - a list of ints (ports) — labels are inferred from DEFAULT_CANDIDATES
    - a list of {port, label} dicts
    If unset, returns DEFAULT_CANDIDATES.
    """
    raw = settings.get("ib_port_candidates")
    if not raw:
        return list(DEFAULT_CANDIDATES)

    label_by_port = {c.port: c.label for c in DEFAULT_CANDIDATES}
    out: list[Candidate] = []
    for item in raw:
        if isinstance(item, int):
            out.append(Candidate(item, label_by_port.get(item, f"port-{item}")))
        elif isinstance(item, dict) and "port" in item:
            port = int(item["port"])
            label = str(item.get("label") or label_by_port.get(port, f"port-{port}"))
            out.append(Candidate(port, label))
        else:
            raise ValueError(f"Unrecognized ib_port_candidates entry: {item!r}")
    return out


def pick_account(mode: str, env_vars: dict, discovered: list[str]) -> str:
    """Pick the configured account_id for the detected mode.

    Prefers the env var for that mode; falls back to the first matching
    account found on the Gateway if the env var is missing.
    """
    if mode == "paper":
        acct = env_vars.get("IB_ACCOUNT_ID_PAPER") or env_vars.get("IB_ACCOUNT_ID")
        fallback = next((a for a in discovered if a.startswith("DU")), None)
    else:
        acct = env_vars.get("IB_ACCOUNT_ID")
        fallback = next((a for a in discovered if not a.startswith("DU")), None)

    if acct and acct in discovered:
        return acct
    if acct:
        raise SystemExit(
            f"Detected mode={mode!r} but configured account {acct!r} is not in "
            f"the Gateway's managed accounts {discovered}. "
            f"Fix {'IB_ACCOUNT_ID_PAPER' if mode == 'paper' else 'IB_ACCOUNT_ID'} "
            f"in .env or switch Gateway accounts."
        )
    if fallback:
        logger.warning(
            '{"event": "ACCOUNT_ID_ENV_MISSING", "mode": "%s", "fallback": "%s"}',
            mode, fallback,
        )
        return fallback
    raise SystemExit(
        f"Detected mode={mode!r} but no matching account_id in .env and no "
        f"matching account on the Gateway. Discovered: {discovered}"
    )


def pick_market_data_type(mode: str, env_vars: dict, settings: dict) -> int:
    """Pick market_data_type for the detected mode.

    Paper accounts must use type 3 (delayed) to avoid IB error 10197
    (competing live session). Live accounts default to 1 (realtime).
    """
    if mode == "paper":
        return int(env_vars.get("IB_MARKET_DATA_TYPE_PAPER",
                                settings.get("ib_market_data_type", 3)))
    return int(env_vars.get("IB_MARKET_DATA_TYPE", 1))
