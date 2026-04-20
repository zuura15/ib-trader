"""Unit tests for .env loading and additional config branches."""
import os
import pytest

from ib_trader.config.loader import load_env, load_settings, load_symbols
from ib_trader.engine.exceptions import ConfigurationError


class TestLoadEnv:
    def test_loads_valid_env(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "IB_HOST=127.0.0.1\nIB_PORT=7497\nIB_CLIENT_ID=1\nIB_ACCOUNT_ID=U1234567\n"
        )
        os.chmod(str(env_file), 0o600)
        env = load_env(str(env_file))
        assert env["IB_HOST"] == "127.0.0.1"
        assert env["IB_ACCOUNT_ID"] == "U1234567"

    def test_missing_env_file_raises(self):
        with pytest.raises(ConfigurationError, match=r"\.env file not found"):
            load_env("/nonexistent/.env")

    def test_wrong_permissions_raises(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("IB_HOST=127.0.0.1\nIB_PORT=7497\nIB_CLIENT_ID=1\nIB_ACCOUNT_ID=U1\n")
        os.chmod(str(env_file), 0o644)  # Wrong permissions
        with pytest.raises(ConfigurationError, match="permissions"):
            load_env(str(env_file))

    def test_missing_env_key_raises(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("IB_HOST=127.0.0.1\n")  # Missing PORT, CLIENT_ID, ACCOUNT_ID
        os.chmod(str(env_file), 0o600)
        with pytest.raises(ConfigurationError, match="missing required keys"):
            load_env(str(env_file))


class TestLoadSettingsEdgeCases:
    def test_non_mapping_yaml_raises(self, tmp_path):
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigurationError, match="must be a YAML mapping"):
            load_settings(str(settings_file))


class TestLoadSymbolsEdgeCases:
    def test_invalid_yaml_raises(self, tmp_path):
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text("key: value: invalid:\n  - bad\n")
        with pytest.raises(ConfigurationError):
            load_symbols(str(symbols_file))

    def test_non_list_yaml_raises(self, tmp_path):
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text("symbol: MSFT\n")
        with pytest.raises(ConfigurationError, match="non-empty YAML list"):
            load_symbols(str(symbols_file))
