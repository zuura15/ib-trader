"""Tests for the process-wide bot registry (registry_config)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ib_trader.bots import registry_config


def _write(dir_: Path, name: str, body: str) -> None:
    (dir_ / name).write_text(body)


@pytest.fixture(autouse=True)
def _clean_registry():
    registry_config.clear()
    yield
    registry_config.clear()


class TestLoadAndLookup:
    def test_load_populates_registry(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: a\nname: alpha\nstrategy: s\n")
        _write(tmp_path, "b.yaml", "id: b\nname: bravo\nstrategy: s\n")
        defs = registry_config.load(tmp_path)
        assert {d.id for d in defs} == {"a", "b"}
        assert {d.id for d in registry_config.all_definitions()} == {"a", "b"}

    def test_get_by_id(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: alpha-id\nname: alpha\nstrategy: s\n")
        registry_config.load(tmp_path)
        bot = registry_config.get("alpha-id")
        assert bot is not None
        assert bot.name == "alpha"

    def test_get_missing_returns_none(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: alpha-id\nname: alpha\nstrategy: s\n")
        registry_config.load(tmp_path)
        assert registry_config.get("nope") is None

    def test_get_by_name(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: 1\nname: alpha\nstrategy: s\n")
        registry_config.load(tmp_path)
        bot = registry_config.get_by_name("alpha")
        assert bot is not None and bot.id == "1"


class TestReload:
    def test_reload_re_reads_same_dir(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: a\nname: alpha\nstrategy: s\n")
        registry_config.load(tmp_path)

        # New file appears on disk
        _write(tmp_path, "b.yaml", "id: b\nname: bravo\nstrategy: s\n")
        defs = registry_config.reload()
        assert {d.id for d in defs} == {"a", "b"}

    def test_reload_picks_up_deletes(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: a\nname: alpha\nstrategy: s\n")
        _write(tmp_path, "b.yaml", "id: b\nname: bravo\nstrategy: s\n")
        registry_config.load(tmp_path)
        (tmp_path / "b.yaml").unlink()
        defs = registry_config.reload()
        assert {d.id for d in defs} == {"a"}

    def test_reload_before_load_raises(self):
        with pytest.raises(RuntimeError, match="has not been loaded"):
            registry_config.reload()


class TestIterationSafety:
    def test_all_definitions_returns_copy(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: a\nname: alpha\nstrategy: s\n")
        registry_config.load(tmp_path)
        lst = registry_config.all_definitions()
        lst.clear()  # mutate caller's copy
        # Registry untouched
        assert len(registry_config.all_definitions()) == 1
