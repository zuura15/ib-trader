"""Tests for the YAML-backed bot config loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from ib_trader.bots.config_loader import (
    BotConfigError, diff_definitions, load_all_bots,
)
from ib_trader.bots.definition import BotDefinition


def _write(dir_: Path, name: str, body: str) -> Path:
    path = dir_ / name
    path.write_text(body)
    return path


class TestLoadAllBots:
    def test_empty_dir_returns_empty_list(self, tmp_path: Path):
        (tmp_path / "config").mkdir()
        assert load_all_bots(tmp_path / "config") == []

    def test_missing_dir_returns_empty_list(self, tmp_path: Path):
        assert load_all_bots(tmp_path / "does-not-exist") == []

    def test_loads_single_bot(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", """
id: 11111111-1111-1111-1111-111111111111
name: alpha
strategy: strategy_bot
tick_interval_seconds: 5
manual_entry_only: true
symbols: [F]
config:
  symbol: F
  qty: 10
""")
        bots = load_all_bots(tmp_path)
        assert len(bots) == 1
        a = bots[0]
        assert a.id == "11111111-1111-1111-1111-111111111111"
        assert a.name == "alpha"
        assert a.strategy == "strategy_bot"
        assert a.tick_interval_seconds == 5
        assert a.manual_entry_only is True
        assert a.symbols == ("F",)
        assert a.config == {"symbol": "F", "qty": 10}
        assert a.source_path.endswith("a.yaml")

    def test_sorted_by_name(self, tmp_path: Path):
        _write(tmp_path, "zzz.yaml", "id: 1\nname: zulu\nstrategy: s\n")
        _write(tmp_path, "aaa.yaml", "id: 2\nname: alpha\nstrategy: s\n")
        _write(tmp_path, "mmm.yaml", "id: 3\nname: mike\nstrategy: s\n")
        names = [b.name for b in load_all_bots(tmp_path)]
        assert names == ["alpha", "mike", "zulu"]

    def test_skips_underscore_and_dot_prefixed(self, tmp_path: Path):
        _write(tmp_path, "_disabled.yaml", "id: 1\nname: x\nstrategy: s\n")
        _write(tmp_path, ".swp.yaml", "id: 2\nname: y\nstrategy: s\n")
        _write(tmp_path, "keep.yaml", "id: 3\nname: keep\nstrategy: s\n")
        names = [b.name for b in load_all_bots(tmp_path)]
        assert names == ["keep"]

    def test_duplicate_id_raises(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: 1\nname: alpha\nstrategy: s\n")
        _write(tmp_path, "b.yaml", "id: 1\nname: bravo\nstrategy: s\n")
        with pytest.raises(BotConfigError, match="duplicate bot id"):
            load_all_bots(tmp_path)

    def test_duplicate_name_raises(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: 1\nname: alpha\nstrategy: s\n")
        _write(tmp_path, "b.yaml", "id: 2\nname: alpha\nstrategy: s\n")
        with pytest.raises(BotConfigError, match="duplicate bot name"):
            load_all_bots(tmp_path)

    def test_missing_required_field_raises(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "name: alpha\nstrategy: s\n")  # no id
        with pytest.raises(BotConfigError, match="missing required field"):
            load_all_bots(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: [unterminated\n")
        with pytest.raises(BotConfigError, match="invalid YAML"):
            load_all_bots(tmp_path)

    def test_scalar_top_level_raises(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "just-a-string\n")
        with pytest.raises(BotConfigError, match="top-level YAML must be a mapping"):
            load_all_bots(tmp_path)

    def test_non_string_id_coerced(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: 42\nname: x\nstrategy: s\n")
        bot = load_all_bots(tmp_path)[0]
        assert bot.id == "42"
        assert isinstance(bot.id, str)

    def test_symbols_scalar_becomes_tuple(self, tmp_path: Path):
        _write(tmp_path, "a.yaml", "id: 1\nname: x\nstrategy: s\nsymbols: F\n")
        bot = load_all_bots(tmp_path)[0]
        assert bot.symbols == ("F",)


class TestDiffDefinitions:
    def _mk(self, bot_id: str, name: str = "n", **overrides) -> BotDefinition:
        return BotDefinition(id=bot_id, name=name, strategy="s", **overrides)

    def test_empty_is_noop(self):
        added, removed, changed = diff_definitions([], [])
        assert added == [] and removed == [] and changed == []

    def test_addition_detected(self):
        new = [self._mk("1")]
        added, removed, changed = diff_definitions([], new)
        assert added == new and removed == [] and changed == []

    def test_removal_detected(self):
        old = [self._mk("1")]
        added, removed, changed = diff_definitions(old, [])
        assert added == [] and removed == old and changed == []

    def test_change_detected_by_content(self):
        old = [self._mk("1", tick_interval_seconds=5)]
        new = [self._mk("1", tick_interval_seconds=10)]
        added, removed, changed = diff_definitions(old, new)
        assert added == [] and removed == []
        assert len(changed) == 1
        assert changed[0][0].tick_interval_seconds == 5
        assert changed[0][1].tick_interval_seconds == 10

    def test_identical_is_noop(self):
        same = self._mk("1")
        added, removed, changed = diff_definitions([same], [same])
        assert added == [] and removed == [] and changed == []


class TestBotDefinitionImmutability:
    def test_frozen(self):
        bot = BotDefinition(id="1", name="x", strategy="s")
        with pytest.raises(Exception):
            bot.name = "y"  # type: ignore[misc]

    def test_symbols_always_tuple(self):
        bot = BotDefinition(id="1", name="x", strategy="s", symbols=["A", "B"])  # type: ignore[arg-type]
        assert isinstance(bot.symbols, tuple)
        assert bot.symbols == ("A", "B")
