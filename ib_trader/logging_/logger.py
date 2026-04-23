"""Structured JSON logger with rotation and compression, plus a crisp
human-readable formatter for stdout.

- File handler: one JSON object per line, rotated & gzipped. Captures DEBUG.
- Stdout handler: ``HH:MM:SS [PREFIX] LEVEL event-or-message k=v k=v``
  with ANSI colors on the level token (red/yellow/green/dim) when stdout
  is a TTY. Captures WARNING+ by default; the file keeps the full stream.
"""
import gzip
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a LogRecord to a JSON string."""
        log_obj: dict[str, Any] = {
            # Server-local timezone for single-user deployment.
            # TODO: Switch to UTC if multi-timezone deployment is needed.
            "timestamp": datetime.now().astimezone().isoformat(),
            "level": record.levelname,
        }

        # Try to parse the message as a JSON fragment (structured event)
        msg = record.getMessage()
        try:
            parsed = json.loads(msg)
            if isinstance(parsed, dict):
                log_obj.update(parsed)
            else:
                log_obj["message"] = msg
        except (json.JSONDecodeError, ValueError):
            log_obj["message"] = msg

        if record.exc_info:
            log_obj["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(log_obj)


# Map module prefixes → compact short tags. Checked in order.
_PREFIX_MAP: tuple[tuple[str, str], ...] = (
    ("ib_trader.engine", "E"),
    ("ib_trader.bots", "B"),
    ("ib_trader.api", "API"),
    ("ib_trader.daemon", "D"),
    ("ib_trader.broker.ib", "IB"),
    ("ib_trader.ib", "IB"),
    ("ib_trader.broker", "BR"),
    ("ib_trader.data", "DATA"),
    ("ib_trader.redis", "R"),
    ("ib_trader.repl", "UI"),
    ("ib_trader.config", "CFG"),
    ("ib_trader.logging_", "LOG"),
)


def _prefix_for(logger_name: str) -> str:
    for key, tag in _PREFIX_MAP:
        if logger_name == key or logger_name.startswith(key + "."):
            return tag
    return "LOG"


# ANSI escapes. Level only — leaves message text at terminal default.
_LEVEL_COLORS = {
    "ERROR": "\033[31m",   # red
    "WARNING": "\033[33m", # yellow
    "INFO": "\033[32m",    # green
    "DEBUG": "\033[2;37m", # dim gray
}
_LEVEL_SHORT = {
    "WARNING": "WARN",
}
_RESET = "\033[0m"


class HumanFormatter(logging.Formatter):
    """Crisp single-line stdout format with optional ANSI colors.

    Shape: ``HH:MM:SS [PREFIX] LEVEL body``

    - ``PREFIX`` is inferred from the logger name (see ``_PREFIX_MAP``).
    - ``LEVEL`` is padded to 5 chars (``WARNING`` abbreviated to ``WARN``)
      and colored when the target stream is a TTY.
    - ``body`` unwraps structured JSON messages: shows the ``event`` token
      and renders remaining fields as ``k=v`` pairs. Plain-string messages
      pass through unchanged.

    Respects ``NO_COLOR`` (https://no-color.org) — if that env var is set
    to any non-empty value, colorization is disabled regardless of TTY.
    """

    def __init__(self, *, colorize: bool | None = None) -> None:
        super().__init__()
        if colorize is None:
            colorize = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
        self.colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        prefix = _prefix_for(record.name)
        raw_level = record.levelname
        short = _LEVEL_SHORT.get(raw_level, raw_level)
        level_str = f"{short:<5}"
        if self.colorize:
            color = _LEVEL_COLORS.get(raw_level, "")
            if color:
                level_str = f"{color}{level_str}{_RESET}"

        body = self._body(record)
        line = f"{ts} [{prefix}] {level_str} {body}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line

    @staticmethod
    def _body(record: logging.LogRecord) -> str:
        msg = record.getMessage()
        try:
            parsed = json.loads(msg)
        except (json.JSONDecodeError, ValueError):
            return msg
        if not isinstance(parsed, dict):
            return msg

        event = parsed.pop("event", None)
        text = parsed.pop("message", None)
        # Drop null-ish noise from extras so lines stay scannable.
        extras = " ".join(
            f"{k}={v}" for k, v in parsed.items()
            if v is not None and v != "None" and v != ""
        )
        parts = [p for p in (event, text, extras) if p]
        return " ".join(parts) or msg


class GzipRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that gzip-compresses rotated log files."""

    def doRollover(self) -> None:
        """Rotate the log file and compress the old file with gzip."""
        super().doRollover()
        # Find the most recently rotated file (baseFilename.1) and compress it
        for i in range(self.backupCount, 0, -1):
            rotated = f"{self.baseFilename}.{i}"
            compressed = f"{rotated}.gz"
            if os.path.exists(rotated) and not os.path.exists(compressed):
                with open(rotated, "rb") as f_in:
                    with gzip.open(compressed, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                os.remove(rotated)


def setup_logging(
    log_file_path: str = "logs/ib_trader.log",
    log_level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
    compress_old: bool = True,
) -> None:
    """Configure structured JSON logging for the application.

    Args:
        log_file_path: Path to the log file (created if it does not exist).
        log_level: Logging level name (DEBUG, INFO, WARNING, ERROR).
        max_bytes: Maximum log file size before rotation.
        backup_count: Number of rotated files to keep.
        compress_old: If True, compress rotated files with gzip.
    """
    log_path = Path(log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    json_formatter = JSONFormatter()
    human_formatter = HumanFormatter()

    # File handler (rotating, with optional gzip compression) — JSON, full detail.
    if compress_old:
        file_handler = GzipRotatingFileHandler(
            log_file_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
    else:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
    file_handler.setFormatter(json_formatter)
    # File always captures DEBUG — the log file is for troubleshooting.
    # The log_level parameter controls stdout verbosity only.
    file_handler.setLevel(logging.DEBUG)

    # Stderr handler — human-readable, warnings+ by default to avoid clutter.
    # (Python's StreamHandler defaults to sys.stderr.)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(human_formatter)
    stream_handler.setLevel(logging.WARNING)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
