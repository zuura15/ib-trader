"""Unit tests for config loading and validation."""
import os
import pytest

from ib_trader.config.loader import (
    load_settings, load_symbols,
    check_file_permissions, validate_symbol,
)
from ib_trader.engine.exceptions import ConfigurationError, SymbolNotAllowedError


class TestLoadSettings:
    def test_loads_valid_settings(self, tmp_path):
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text("""
max_order_size_shares: 10
max_retries: 3
retry_delay_seconds: 2
retry_backoff_multiplier: 2.0
reprice_steps: 10
reprice_active_duration_seconds: 30
reprice_passive_wait_seconds: 90
ib_host: 127.0.0.1
ib_port: 7497
ib_client_id: 1
ib_min_call_interval_ms: 100
cache_ttl_seconds: 86400
log_level: INFO
log_file_path: logs/ib_trader.log
log_rotation_max_bytes: 10485760
log_rotation_backup_count: 10
log_compress_old: true
heartbeat_interval_seconds: 30
heartbeat_stale_threshold_seconds: 300
reconciliation_interval_seconds: 1800
db_integrity_check_interval_seconds: 21600
daemon_tui_refresh_seconds: 5
""")
        settings = load_settings(str(settings_file))
        assert settings["max_order_size_shares"] == 10
        assert settings["ib_port"] == 7497

    def test_missing_file_raises(self):
        with pytest.raises(ConfigurationError, match=r"settings\.yaml not found"):
            load_settings("/nonexistent/path/settings.yaml")

    def test_missing_key_raises(self, tmp_path):
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text("max_order_size_shares: 10\n")
        with pytest.raises(ConfigurationError, match="missing required keys"):
            load_settings(str(settings_file))

    def test_invalid_yaml_raises(self, tmp_path):
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text("invalid: yaml: :\n")
        with pytest.raises(ConfigurationError):
            load_settings(str(settings_file))


class TestLoadSymbols:
    def test_loads_valid_symbols(self, tmp_path):
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text("- MSFT\n- AAPL\n- GOOGL\n")
        symbols = load_symbols(str(symbols_file))
        assert "MSFT" in symbols
        assert "AAPL" in symbols
        assert len(symbols) == 3

    def test_lowercase_uppercased(self, tmp_path):
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text("- msft\n- aapl\n")
        symbols = load_symbols(str(symbols_file))
        assert "MSFT" in symbols

    def test_missing_file_raises(self):
        with pytest.raises(ConfigurationError, match=r"symbols\.yaml not found"):
            load_symbols("/nonexistent/symbols.yaml")

    def test_empty_list_raises(self, tmp_path):
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text("[]\n")
        with pytest.raises(ConfigurationError):
            load_symbols(str(symbols_file))


class TestCheckFilePermissions:
    def test_correct_permissions_passes(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("KEY=val")
        os.chmod(str(f), 0o600)
        # Should not raise
        check_file_permissions(str(f), 0o600, ".env")

    def test_wrong_permissions_raises(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text("KEY=val")
        os.chmod(str(f), 0o644)
        with pytest.raises(ConfigurationError, match="permissions"):
            check_file_permissions(str(f), 0o600, ".env")

    def test_missing_file_raises(self):
        with pytest.raises(ConfigurationError, match="not found"):
            check_file_permissions("/nonexistent/.env", 0o600, ".env")


class TestValidateSymbol:
    def test_valid_symbol_passes(self):
        validate_symbol("MSFT", ["MSFT", "AAPL"])  # Should not raise

    def test_invalid_symbol_raises(self):
        with pytest.raises(SymbolNotAllowedError):
            validate_symbol("INVALID", ["MSFT", "AAPL"])

    def test_case_insensitive(self):
        validate_symbol("msft", ["MSFT", "AAPL"])  # Should not raise

    def test_empty_whitelist_raises(self):
        with pytest.raises(SymbolNotAllowedError):
            validate_symbol("MSFT", [])
