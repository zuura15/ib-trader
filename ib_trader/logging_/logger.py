"""Structured JSON logger with rotation and compression.

Sets up Python's logging to emit one JSON object per line.
Rotates at max_bytes, keeps backup_count files, compresses old files with gzip.
Logs to both file and stdout.
"""
import gzip
import json
import logging
import logging.handlers
import os
import shutil
from datetime import datetime, timezone
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

    formatter = JSONFormatter()
    level = getattr(logging, log_level.upper(), logging.INFO)

    # File handler (rotating, with optional gzip compression)
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
    file_handler.setFormatter(formatter)
    # File always captures DEBUG — the log file is for troubleshooting.
    # The log_level parameter controls stdout verbosity only.
    file_handler.setLevel(logging.DEBUG)

    # Stdout handler
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.WARNING)  # Only warnings+ to stdout to avoid cluttering REPL

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
