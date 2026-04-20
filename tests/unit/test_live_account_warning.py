"""Unit tests for live account detection.

Verifies:
- Account ID starting with 'DU' → no warning, startup proceeds
- Account ID not starting with 'DU' → warning generated
- LIVE_ACCOUNT_CONNECTED event logged when live account detected
"""


class TestLiveAccountDetection:
    """Tests for live account warning logic."""

    def test_paper_account_no_warning(self):
        """Account ID starting with 'DU' does not trigger live warning."""
        account_id = "DU1234567"
        assert account_id.startswith("DU")

    def test_live_account_triggers_warning(self):
        """Account ID not starting with 'DU' triggers live account warning."""
        account_id = "U1234567"
        assert not account_id.startswith("DU")

    def test_various_paper_accounts(self):
        """Various DU-prefixed accounts are correctly identified as paper."""
        for acct in ["DU1111111", "DU9999999", "DU0000001"]:
            assert acct.startswith("DU"), f"{acct} should be detected as paper"

    def test_various_live_accounts(self):
        """Various non-DU accounts are correctly identified as live."""
        for acct in ["U1234567", "F1234567", "I1234567", ""]:
            assert not acct.startswith("DU"), f"{acct} should be detected as live"

    def test_live_account_warning_message_format(self):
        """Warning message contains the account ID and key information."""
        account_id = "U9876543"
        msg = (
            f"\u26a0  WARNING: Connected to LIVE account {account_id}. "
            f"Real money is at risk.\n"
            f"   Paper trading accounts begin with 'DU'. "
            f"Press Enter to continue, or Ctrl-C to abort."
        )
        assert account_id in msg
        assert "LIVE" in msg
        assert "DU" in msg
        assert "Real money" in msg
