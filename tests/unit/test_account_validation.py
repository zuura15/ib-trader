"""Unit tests for engine account-ID validation on startup.

Covers _validate_account_id (Gateway-managedAccounts check) and
_validate_account_mode (paper/live flag vs account prefix sanity).
Both run immediately after IB.connect() in engine.main.run_engine.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ib_trader.engine.main import _validate_account_id, _validate_account_mode


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
        # Some Gateway sessions expose multiple accounts (master + subs);
        # as long as configured is one of them, we accept.
        ctx = _ctx("DU1234567", _FakeIB(managed=["DU0000000", "DU1234567"]))
        _validate_account_id(ctx)


class TestValidateAccountMode:
    def test_paper_flag_with_du_account_passes(self):
        ctx = _ctx("DU1234567", _FakeIB(), mode="paper")
        _validate_account_mode(ctx)

    def test_live_flag_with_u_account_passes(self):
        ctx = _ctx("U9876543", _FakeIB(), mode="live")
        _validate_account_mode(ctx)

    def test_paper_flag_with_live_account_raises(self):
        ctx = _ctx("U9876543", _FakeIB(), mode="paper")
        with pytest.raises(SystemExit, match="does not start with 'DU'"):
            _validate_account_mode(ctx)

    def test_live_flag_with_paper_account_raises(self):
        ctx = _ctx("DU1234567", _FakeIB(), mode="live")
        with pytest.raises(SystemExit, match="starts with 'DU'"):
            _validate_account_mode(ctx)
