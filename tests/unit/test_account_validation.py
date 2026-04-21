"""Unit tests for engine account-ID validation on startup.

Covers _validate_account_id (Gateway-managedAccounts check). The old
_validate_account_mode (paper/live flag vs account prefix sanity) was
removed when the engine switched to auto-detecting mode from the Gateway
— see ADR 015. See tests/unit/test_gateway_mode_detection.py for the
replacement coverage.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ib_trader.engine.main import _validate_account_id


class _FakeIB:
    def __init__(self, managed: list[str] | None = None, *, raise_on_read: bool = False) -> None:
        self._managed = managed if managed is not None else ["DU0000000"]
        self._raise = raise_on_read

    def managed_accounts(self) -> list[str]:
        if self._raise:
            raise RuntimeError("simulated Gateway read failure")
        return self._managed


def _ctx(account_id: str, ib: _FakeIB, mode: str = "paper"):
    return SimpleNamespace(
        account_id=account_id,
        ib=ib,
        settings={"account_mode": mode},
    )


class TestValidateAccountId:
    def test_matching_account_passes(self):
        ctx = _ctx("DU1234567", _FakeIB(managed=["DU1234567"]))
        _validate_account_id(ctx)  # no raise

    def test_mismatched_account_raises(self):
        ctx = _ctx("DU9999999", _FakeIB(managed=["DU1234567"]))
        with pytest.raises(SystemExit, match="NOT in the Gateway's managed accounts"):
            _validate_account_id(ctx)

    def test_empty_managed_list_raises(self):
        ctx = _ctx("DU1234567", _FakeIB(managed=[]))
        with pytest.raises(SystemExit, match="no managed accounts"):
            _validate_account_id(ctx)

    def test_read_failure_raises(self):
        ctx = _ctx("DU1234567", _FakeIB(raise_on_read=True))
        with pytest.raises(SystemExit, match="Could not read managedAccounts"):
            _validate_account_id(ctx)

    def test_multiple_managed_accounts_any_match(self):
        ctx = _ctx("DU1234567", _FakeIB(managed=["DU0000000", "DU1234567"]))
        _validate_account_id(ctx)
