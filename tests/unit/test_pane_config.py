"""Unit tests for pane_config.py — PaneConfig loading and validation."""
import pytest

from ib_trader.repl.pane_config import PaneName, PaneConfig, load_pane_configs, _DEFAULTS


def settings_with_panes(panes: list[dict]) -> dict:
    return {"tui": {"panes": panes}}


def empty_settings() -> dict:
    return {}


# ---------------------------------------------------------------------------
# Default behaviour
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_empty_settings_returns_all_defaults(self):
        configs = load_pane_configs(empty_settings())
        assert len(configs) == len(_DEFAULTS)

    def test_defaults_sorted_by_rank(self):
        configs = load_pane_configs(empty_settings())
        ranks = [c.rank for c in configs]
        assert ranks == sorted(ranks)

    def test_default_includes_all_pane_names(self):
        configs = load_pane_configs(empty_settings())
        names = {c.name for c in configs}
        assert names == {PaneName.HEADER, PaneName.LOG, PaneName.POSITIONS,
                         PaneName.COMMAND, PaneName.ORDERS}

    def test_header_height_always_1(self):
        configs = load_pane_configs(empty_settings())
        header = next(c for c in configs if c.name == PaneName.HEADER)
        assert header.height == 1

    def test_header_height_forced_to_1_even_if_overridden(self):
        configs = load_pane_configs(settings_with_panes([
            {"name": "header", "rank": 1, "height": 5, "enabled": True}
        ]))
        header = next(c for c in configs if c.name == PaneName.HEADER)
        assert header.height == 1


# ---------------------------------------------------------------------------
# Overrides from settings
# ---------------------------------------------------------------------------

class TestOverrides:
    def test_height_override_applied(self):
        configs = load_pane_configs(settings_with_panes([
            {"name": "log", "rank": 2, "height": 20, "enabled": True}
        ]))
        log = next(c for c in configs if c.name == PaneName.LOG)
        assert log.height == 20

    def test_rank_override_changes_order(self):
        # Move orders to rank 1 (above header's rank 1 — but orders gets 0)
        configs = load_pane_configs(settings_with_panes([
            {"name": "orders", "rank": 0, "height": 10, "enabled": True}
        ]))
        assert configs[0].name == PaneName.ORDERS

    def test_disabled_pane_excluded(self):
        configs = load_pane_configs(settings_with_panes([
            {"name": "positions", "enabled": False}
        ]))
        names = {c.name for c in configs}
        assert PaneName.POSITIONS not in names

    def test_partial_override_keeps_other_defaults(self):
        configs = load_pane_configs(settings_with_panes([
            {"name": "log", "height": 15}
        ]))
        log = next(c for c in configs if c.name == PaneName.LOG)
        assert log.height == 15
        # rank and enabled should stay default
        assert log.rank == 2
        assert log.enabled is True


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class TestValidation:
    def test_fewer_than_2_enabled_raises(self):
        # Disable all but one
        panes = [
            {"name": "header",    "enabled": False},
            {"name": "log",       "enabled": False},
            {"name": "positions", "enabled": False},
            {"name": "command",   "enabled": False},
            {"name": "orders",    "enabled": True},
        ]
        with pytest.raises(ValueError, match="at least 2"):
            load_pane_configs(settings_with_panes(panes))

    def test_duplicate_ranks_raises(self):
        panes = [
            {"name": "log",  "rank": 2},
            {"name": "command", "rank": 2},
        ]
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            load_pane_configs(settings_with_panes(panes))

    def test_unknown_pane_name_in_settings_ignored(self):
        # Should not raise — unknown names silently ignored
        configs = load_pane_configs(settings_with_panes([
            {"name": "nonexistent", "rank": 99, "height": 5, "enabled": True}
        ]))
        assert len(configs) == 5  # All defaults still present


# ---------------------------------------------------------------------------
# PaneConfig immutability
# ---------------------------------------------------------------------------

class TestPaneConfigFrozen:
    def test_pane_config_is_frozen(self):
        cfg = PaneConfig(name=PaneName.LOG, rank=2, height=10, enabled=True)
        with pytest.raises((AttributeError, TypeError)):
            cfg.rank = 99  # type: ignore[misc]
