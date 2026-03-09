"""Configuration loading and validation.

Loads settings.yaml and .env, validates required fields,
and checks file permissions on startup.
"""
import logging
import os
import stat
from pathlib import Path

import yaml
from dotenv import dotenv_values

from ib_trader.engine.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

REQUIRED_SETTINGS_KEYS = [
    "max_order_size_shares",
    "max_retries",
    "retry_delay_seconds",
    "retry_backoff_multiplier",
    "reprice_interval_seconds",
    "reprice_duration_seconds",
    "ib_host",
    "ib_port",
    "ib_client_id",
    "ib_min_call_interval_ms",
    "cache_ttl_seconds",
    "log_level",
    "log_file_path",
    "log_rotation_max_bytes",
    "log_rotation_backup_count",
    "log_compress_old",
    "heartbeat_interval_seconds",
    "heartbeat_stale_threshold_seconds",
    "reconciliation_interval_seconds",
    "db_integrity_check_interval_seconds",
    "daemon_tui_refresh_seconds",
]

REQUIRED_ENV_KEYS = ["IB_HOST", "IB_PORT", "IB_CLIENT_ID", "IB_ACCOUNT_ID"]


def load_settings(settings_path: str = "config/settings.yaml") -> dict:
    """Load and validate settings.yaml.

    Args:
        settings_path: Path to settings.yaml relative to project root.

    Returns:
        Validated settings dict.

    Raises:
        ConfigurationError: If file is missing, unreadable, or missing required keys.
    """
    path = Path(settings_path)
    if not path.exists():
        raise ConfigurationError(f"settings.yaml not found at {path.absolute()}")

    try:
        with open(path) as f:
            settings = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in settings.yaml: {e}") from e

    if not isinstance(settings, dict):
        raise ConfigurationError("settings.yaml must be a YAML mapping")

    missing = [k for k in REQUIRED_SETTINGS_KEYS if k not in settings]
    if missing:
        raise ConfigurationError(f"settings.yaml missing required keys: {missing}")

    logger.info('{"event": "HEALTH_CHECK_PASSED", "check": "settings_yaml"}')
    return settings


def load_env(env_path: str = ".env") -> dict:
    """Load and validate .env file.

    Args:
        env_path: Path to .env file.

    Returns:
        Dict of environment variables from .env.

    Raises:
        ConfigurationError: If file is missing, has wrong permissions, or missing keys.
    """
    path = Path(env_path)
    if not path.exists():
        raise ConfigurationError(
            f".env file not found at {path.absolute()}. "
            "Create it from .env.example and set IB connection details."
        )

    check_file_permissions(str(path), required_mode=0o600, label=".env")

    env = dotenv_values(env_path)
    missing = [k for k in REQUIRED_ENV_KEYS if k not in env or not env[k]]
    if missing:
        raise ConfigurationError(f".env missing required keys: {missing}")

    logger.info('{"event": "HEALTH_CHECK_PASSED", "check": "env_file"}')
    return dict(env)


def load_symbols(symbols_path: str = "config/symbols.yaml") -> list[str]:
    """Load the symbol whitelist from symbols.yaml.

    Args:
        symbols_path: Path to symbols.yaml.

    Returns:
        List of uppercase symbol strings.

    Raises:
        ConfigurationError: If file is missing, invalid, or empty.
    """
    path = Path(symbols_path)
    if not path.exists():
        raise ConfigurationError(f"symbols.yaml not found at {path.absolute()}")

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in symbols.yaml: {e}") from e

    if not data or not isinstance(data, list):
        raise ConfigurationError("symbols.yaml must be a non-empty YAML list of symbols")

    symbols = [str(s).upper() for s in data]
    if not symbols:
        raise ConfigurationError("symbols.yaml must contain at least one symbol")

    logger.info(
        '{"event": "HEALTH_CHECK_PASSED", "check": "symbols_yaml", "count": %d}',
        len(symbols),
    )
    return symbols


def check_file_permissions(path: str, required_mode: int, label: str) -> None:
    """Verify a file has the required Unix permissions.

    Args:
        path: File path to check.
        required_mode: Required mode bits (e.g. 0o600).
        label: Human-readable label for error messages.

    Raises:
        ConfigurationError: If file does not exist or has wrong permissions.
    """
    try:
        file_stat = os.stat(path)
    except FileNotFoundError:
        raise ConfigurationError(f"{label} file not found: {path}")

    actual_mode = stat.S_IMODE(file_stat.st_mode)
    if actual_mode != required_mode:
        raise ConfigurationError(
            f"{label} file permissions are {oct(actual_mode)} — must be {oct(required_mode)}. "
            f"Fix with: chmod {oct(required_mode)[2:]} {path}"
        )


def validate_symbol(symbol: str, whitelist: list[str]) -> None:
    """Validate that a symbol is in the whitelist.

    Args:
        symbol: Symbol to validate (case-insensitive).
        whitelist: List of allowed symbols.

    Raises:
        SymbolNotAllowedError: If symbol is not in the whitelist.
    """
    from ib_trader.engine.exceptions import SymbolNotAllowedError

    if symbol.upper() not in [s.upper() for s in whitelist]:
        raise SymbolNotAllowedError(
            f"Symbol '{symbol}' is not in the whitelist. "
            f"Add it to config/symbols.yaml to enable trading."
        )
