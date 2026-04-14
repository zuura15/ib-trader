"""Tests for bootstrap_bots_from_yaml — the YAML→SQLite sync.

Invariants:
  - Fresh run: adds every YAML bot, no existing rows.
  - Idempotent: running twice with same YAML → second run is all
    "unchanged".
  - YAML edit to mutable field (name, tick_interval) on a STOPPED bot
    → SQLite row updated.
  - YAML edit to immutable field (strategy, ref_id, symbol) on a RUNNING
    bot → BootstrapError, no DB change.
  - YAML deleted for STOPPED bot → BootstrapError unless force=True.
  - YAML deleted for RUNNING bot → BootstrapError even with force=False
    (the running-bot clause fires first).
  - Config version is a stable hash that differs for different configs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.bots import registry_config
from ib_trader.bots.bootstrap import (
    BootstrapError, _config_version, bootstrap_bots_from_yaml,
    config_version_for,
)
from ib_trader.bots.definition import BotDefinition
from ib_trader.data.models import Base, Bot, BotStatus
from ib_trader.data.repositories.bot_repository import BotRepository


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = scoped_session(sessionmaker(bind=engine))
    yield factory
    factory.remove()
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_registry():
    registry_config.clear()
    yield
    registry_config.clear()


def _write_yaml(dir_: Path, name: str, body: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / name).write_text(body)


def _existing_bot_row(sf, bot_id: str) -> Bot | None:
    return BotRepository(sf).get(bot_id)


class TestFreshBootstrap:
    def test_inserts_from_empty_db(self, tmp_path: Path, session_factory):
        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s\n")
        _write_yaml(tmp_path, "b.yaml", "id: b1\nname: bravo\nstrategy: s\n")

        report = bootstrap_bots_from_yaml(session_factory, tmp_path)

        assert set(report.added) == {"a1", "b1"}
        assert report.updated == []
        assert report.unchanged == []
        assert report.removed == []

        assert _existing_bot_row(session_factory, "a1") is not None
        assert _existing_bot_row(session_factory, "b1") is not None

    def test_empty_yaml_dir_is_noop_when_db_empty(self, tmp_path: Path, session_factory):
        report = bootstrap_bots_from_yaml(session_factory, tmp_path)
        assert report.changed_count == 0


class TestIdempotent:
    def test_second_run_unchanged(self, tmp_path: Path, session_factory):
        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s\n")
        bootstrap_bots_from_yaml(session_factory, tmp_path)

        report = bootstrap_bots_from_yaml(session_factory, tmp_path)
        assert report.added == []
        assert report.unchanged == ["a1"]


class TestMutableEdits:
    def test_rename_stopped_bot_accepted(self, tmp_path: Path, session_factory):
        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s\n")
        bootstrap_bots_from_yaml(session_factory, tmp_path)

        # Edit: change name (mutable field)
        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha-renamed\nstrategy: s\n")
        report = bootstrap_bots_from_yaml(session_factory, tmp_path)
        assert report.updated == ["a1"]
        assert _existing_bot_row(session_factory, "a1").name == "alpha-renamed"

    def test_tick_interval_change_on_stopped_bot(self, tmp_path: Path, session_factory):
        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s\ntick_interval_seconds: 5\n")
        bootstrap_bots_from_yaml(session_factory, tmp_path)

        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s\ntick_interval_seconds: 15\n")
        bootstrap_bots_from_yaml(session_factory, tmp_path)
        assert _existing_bot_row(session_factory, "a1").tick_interval_seconds == 15


class TestRunningBotSafety:
    def _set_running(self, session_factory, bot_id: str) -> None:
        BotRepository(session_factory).update_status(bot_id, BotStatus.RUNNING)

    def test_rename_running_bot_accepted(self, tmp_path: Path, session_factory):
        # Name is mutable even while running.
        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s\n")
        bootstrap_bots_from_yaml(session_factory, tmp_path)
        self._set_running(session_factory, "a1")

        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha-2\nstrategy: s\n")
        report = bootstrap_bots_from_yaml(session_factory, tmp_path)
        assert "a1" in report.updated
        assert _existing_bot_row(session_factory, "a1").name == "alpha-2"

    def test_strategy_change_on_running_bot_refused(self, tmp_path: Path, session_factory):
        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s1\n")
        bootstrap_bots_from_yaml(session_factory, tmp_path)
        self._set_running(session_factory, "a1")

        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s2\n")
        with pytest.raises(BootstrapError, match="immutable fields"):
            bootstrap_bots_from_yaml(session_factory, tmp_path)

        # DB unchanged
        assert _existing_bot_row(session_factory, "a1").strategy == "s1"

    def test_ref_id_change_on_running_bot_refused(self, tmp_path: Path, session_factory):
        _write_yaml(tmp_path, "a.yaml", """
id: a1
name: alpha
strategy: s
config:
  ref_id: old-ref
""")
        bootstrap_bots_from_yaml(session_factory, tmp_path)
        self._set_running(session_factory, "a1")

        _write_yaml(tmp_path, "a.yaml", """
id: a1
name: alpha
strategy: s
config:
  ref_id: new-ref
""")
        with pytest.raises(BootstrapError, match="ref_id"):
            bootstrap_bots_from_yaml(session_factory, tmp_path)

    def test_symbol_change_on_running_bot_refused(self, tmp_path: Path, session_factory):
        _write_yaml(tmp_path, "a.yaml", """
id: a1
name: alpha
strategy: s
config:
  symbol: F
""")
        bootstrap_bots_from_yaml(session_factory, tmp_path)
        self._set_running(session_factory, "a1")

        _write_yaml(tmp_path, "a.yaml", """
id: a1
name: alpha
strategy: s
config:
  symbol: QQQ
""")
        with pytest.raises(BootstrapError, match="symbol"):
            bootstrap_bots_from_yaml(session_factory, tmp_path)


class TestOrphanSqliteRows:
    def test_refuses_orphan_without_force(self, tmp_path: Path, session_factory):
        # Seed a row that has no YAML.
        repo = BotRepository(session_factory)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        repo.create(Bot(
            id="orphan", name="orphan", strategy="s",
            broker="ib", config_json="{}", tick_interval_seconds=10,
            created_at=now, updated_at=now,
        ))

        with pytest.raises(BootstrapError, match="no matching YAML"):
            bootstrap_bots_from_yaml(session_factory, tmp_path)

    def test_force_deletes_orphan_of_stopped_bot(self, tmp_path: Path, session_factory):
        repo = BotRepository(session_factory)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        repo.create(Bot(
            id="orphan", name="orphan", strategy="s",
            broker="ib", config_json="{}", tick_interval_seconds=10,
            status=BotStatus.STOPPED, created_at=now, updated_at=now,
        ))

        report = bootstrap_bots_from_yaml(session_factory, tmp_path, force=True)
        assert report.removed == ["orphan"]
        assert _existing_bot_row(session_factory, "orphan") is None

    def test_running_orphan_refused_even_with_force(self, tmp_path: Path, session_factory):
        repo = BotRepository(session_factory)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        repo.create(Bot(
            id="orphan", name="orphan", strategy="s",
            broker="ib", config_json="{}", tick_interval_seconds=10,
            status=BotStatus.RUNNING, created_at=now, updated_at=now,
        ))

        # Running-bot clause fires first — operator must stop the bot.
        with pytest.raises(BootstrapError, match="RUNNING"):
            bootstrap_bots_from_yaml(session_factory, tmp_path)


class TestConfigVersion:
    def test_stable(self):
        d = BotDefinition(id="a", name="a", strategy="s", config={"qty": 10})
        assert _config_version(d) == _config_version(d)

    def test_different_configs_differ(self):
        a = BotDefinition(id="a", name="a", strategy="s", config={"qty": 10})
        b = BotDefinition(id="a", name="a", strategy="s", config={"qty": 11})
        assert _config_version(a) != _config_version(b)

    def test_lookup_after_load(self, tmp_path: Path, session_factory):
        _write_yaml(tmp_path, "a.yaml", "id: a1\nname: alpha\nstrategy: s\n")
        bootstrap_bots_from_yaml(session_factory, tmp_path)
        assert config_version_for("a1") is not None
        assert config_version_for("nope") is None
