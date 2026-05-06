"""Environment detection from hostname.

Two boxes connect to the same IB Gateway:
  • prod box (hostname ``local-prod``): runs the Gateway + the prod
    trading server. Uses IB client ID 1.
  • dev box (any other hostname): development / staging that connects
    remotely to the same Gateway. Uses IB client ID 2.

Both connect to the same IB account. The orderRef tags include the
hostname so the reconciler / orphan-detection logic can ignore orders
placed by the *other* instance — IB shows all account orders to all
connected clients, but each client should only own its own.

Single source of truth so client_id and orderRef prefix can never
disagree.
"""
import socket

#: Hostname of the prod box. Anything else is treated as dev.
PROD_HOSTNAME = "local-prod"

#: IB client IDs per environment. Two boxes connecting to the same
#: Gateway must use distinct client IDs or the second connect is
#: rejected.
_CLIENT_ID_BY_ENV = {
    "prod": 1,
    "dev": 2,
}


def hostname() -> str:
    """Local hostname, sanitized for orderRef (no colon — that's the
    field separator). Falls back to ``unknown`` if socket lookup
    fails for any reason."""
    try:
        h = socket.gethostname() or "unknown"
    except OSError:
        h = "unknown"
    return h.replace(":", "-")


def env() -> str:
    """Returns ``"prod"`` if running on the prod box, else ``"dev"``."""
    return "prod" if hostname() == PROD_HOSTNAME else "dev"


def default_client_id() -> int:
    """IB client ID for this box. The IB_CLIENT_ID env var still
    overrides this in the engine/REPL/daemon — this is the default
    when the env var isn't set."""
    return _CLIENT_ID_BY_ENV[env()]
