#!/usr/bin/env -S uv run python
"""One-time migration: dump SQLite ``bots`` rows to ``config/bots/*.yaml``.

Run once after upgrading to the YAML-authoritative refactor. After
this runs, `ib-api` / `ib-bots` startup will bootstrap successfully
because every SQLite row now has a matching YAML on disk.

Usage:
    uv run python scripts/migrate_bots_to_yaml.py
    uv run python scripts/migrate_bots_to_yaml.py --db trader.db --out config/bots --dry-run
    uv run python scripts/migrate_bots_to_yaml.py --overwrite  # replace existing YAMLs

Behaviour:
    - Writes one file per bot at <out>/<name-slug>.yaml.
    - Fails if the target file already exists (use --overwrite to force).
    - Emits the same fields the bootstrap expects: id, name, strategy,
      broker, tick_interval_seconds, manual_entry_only (default False),
      config (the parsed config_json blob).
    - Does NOT touch SQLite. The migration is one-way — after the YAMLs
      are correct, you can optionally delete the SQLite rows and let
      bootstrap recreate them on next startup.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.repositories.bot_repository import BotRepository


_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _slugify(name: str) -> str:
    """Turn a bot's display name into a filesystem-safe filename stem."""
    s = _SLUG_RE.sub("-", name.strip()).strip("-").lower()
    return s or "bot"


def _bot_to_yaml_dict(bot) -> dict:
    """Shape a SQLAlchemy Bot row as the dict we serialize to YAML.

    The key ordering here is deliberate — it's what operators will
    read when they open the file, so the important bits come first.
    """
    try:
        cfg = json.loads(bot.config_json) if bot.config_json else {}
    except json.JSONDecodeError:
        cfg = {"_raw_config_json": bot.config_json}

    payload = {
        "id": bot.id,
        "name": bot.name,
        "strategy": bot.strategy,
        "broker": bot.broker or "ib",
        "tick_interval_seconds": int(bot.tick_interval_seconds or 10),
        "manual_entry_only": False,
        "config": cfg,
    }
    symbols = cfg.get("symbol")
    if symbols:
        # Informational only; the strategy's symbol also lives in `config`.
        payload["symbols"] = [symbols] if isinstance(symbols, str) else list(symbols)
    return payload


def migrate(db_path: str, out_dir: Path, *, dry_run: bool, overwrite: bool) -> int:
    """Run the export. Returns exit code (0 ok, non-zero on refusal)."""
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    factory = scoped_session(sessionmaker(bind=engine))
    try:
        repo = BotRepository(factory)
        bots = repo.get_all()
    finally:
        factory.remove()
        engine.dispose()

    if not bots:
        print(f"[migrate] no bots found in {db_path}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)

    refusals = 0
    written = 0
    for bot in bots:
        slug = _slugify(bot.name)
        path = out_dir / f"{slug}.yaml"
        if path.exists() and not overwrite:
            print(
                f"[migrate] SKIP  {path} already exists "
                f"(use --overwrite to replace)",
                file=sys.stderr,
            )
            refusals += 1
            continue

        payload = _bot_to_yaml_dict(bot)
        text = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
        if dry_run:
            print(f"[migrate] DRY   would write {path} ({len(text)} bytes)")
            print(text)
        else:
            path.write_text(text)
            written += 1
            print(f"[migrate] WRITE {path}  ({bot.name} -> {bot.strategy})")

    verb = "would write" if dry_run else "wrote"
    print(
        f"[migrate] done — {verb} {written}/{len(bots)} files, "
        f"refused {refusals}."
    )
    if refusals:
        print(
            "[migrate] refusals prevent the migration from being complete. "
            "Either delete/rename the conflicting YAMLs or re-run with "
            "--overwrite.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="trader.db", help="SQLite database path")
    parser.add_argument(
        "--out", default="config/bots", help="Output directory for YAML files",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be written without touching disk.",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Replace existing YAML files instead of skipping them.",
    )
    args = parser.parse_args(argv)
    return migrate(
        args.db, Path(args.out),
        dry_run=args.dry_run, overwrite=args.overwrite,
    )


if __name__ == "__main__":
    raise SystemExit(main())
