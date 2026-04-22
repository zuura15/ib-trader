"""Pane layout configuration for the REPL TUI.

Loads pane definitions from the ``tui.panes`` block in settings.yaml.
Falls back to built-in defaults for any pane not overridden.

Validation rules:
- At least 2 enabled panes must be present.
- Rank values must be unique across enabled panes.
- The HEADER pane height is always forced to 1 row.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PaneName(Enum):
    """Identifiers for the five standard TUI panes."""

    HEADER = "header"
    LOG = "log"
    POSITIONS = "positions"
    COMMAND = "command"
    ORDERS = "orders"


@dataclass(frozen=True)
class PaneConfig:
    """Immutable configuration for a single TUI pane.

    Attributes:
        name: Pane identifier.
        rank: Vertical display order (lower = higher in layout).  Must be
            unique across all enabled panes.
        height: Terminal rows allocated to this pane.  For HEADER this is
            always forced to 1 regardless of the configured value.
        enabled: Whether the pane is included in the layout.
    """

    name: PaneName
    rank: int
    height: int
    enabled: bool


# Built-in defaults used when no ``tui.panes`` block is present in settings.yaml.
_DEFAULTS: list[dict[str, Any]] = [
    {"name": "header",    "rank": 1, "height": 1,  "enabled": True},
    {"name": "log",       "rank": 2, "height": 10, "enabled": True},
    {"name": "positions", "rank": 3, "height": 10, "enabled": True},
    {"name": "command",   "rank": 4, "height": 5,  "enabled": True},
    {"name": "orders",    "rank": 5, "height": 10, "enabled": True},
]


def load_pane_configs(settings: dict[str, Any]) -> list[PaneConfig]:
    """Load pane configurations from the settings dict.

    Merges the ``tui.panes`` block from settings with built-in defaults.
    Any pane not mentioned in settings uses its default values.  Unknown pane
    names in the settings block are silently ignored.

    Args:
        settings: Full settings dict loaded from settings.yaml.

    Returns:
        List of enabled PaneConfig objects sorted by rank (ascending).

    Raises:
        ValueError: If fewer than 2 panes are enabled, or if two enabled
            panes share the same rank.
    """
    tui_block = settings.get("tui", {})
    overrides: dict[str, dict[str, Any]] = {}
    for entry in tui_block.get("panes", []):
        name = entry.get("name")
        if name:
            overrides[name] = entry

    configs: list[PaneConfig] = []
    for default in _DEFAULTS:
        name_str: str = default["name"]
        override = overrides.get(name_str, {})

        try:
            pane_name = PaneName(name_str)
        except ValueError:
            continue  # Should never happen with _DEFAULTS, but be defensive.

        rank: int = int(override.get("rank", default["rank"]))
        height: int = int(override.get("height", default["height"]))
        enabled: bool = bool(override.get("enabled", default["enabled"]))

        # Header is always exactly 1 row.
        if pane_name == PaneName.HEADER:
            height = 1

        configs.append(PaneConfig(name=pane_name, rank=rank, height=height, enabled=enabled))

    enabled_configs = [c for c in configs if c.enabled]

    if len(enabled_configs) < 2:
        raise ValueError(
            f"TUI requires at least 2 enabled panes, got {len(enabled_configs)}: "
            f"{[c.name.value for c in enabled_configs]}"
        )

    ranks = [c.rank for c in enabled_configs]
    if len(ranks) != len(set(ranks)):
        seen: set[int] = set()
        dupes = sorted(r for r in ranks if r in seen or seen.add(r))  # type: ignore[func-returns-value]
        raise ValueError(f"Duplicate pane ranks in TUI config: {dupes}")

    return sorted(enabled_configs, key=lambda c: c.rank)
