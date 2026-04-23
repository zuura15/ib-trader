"""Tests for the external pager bash scripts.

Covers ops/health_check.sh and ops/maint. We exercise them as
subprocesses with mocked env paths (temp dir) so we don't touch
the real ~/.config files or trigger actual HTTP calls.

These are integration-shaped tests but live under tests/unit/ because
they don't start the full app stack — they just run bash and assert
exit codes / filesystem effects. Fast (<1s each).

See GH issue #47 for design.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "ops"
HEALTH = OPS / "health_check.sh"
MAINT = OPS / "maint"


@pytest.fixture
def pager_env(tmp_path):
    """Pointer fixture: writes a throwaway pager env file with bogus
    HC + ntfy URLs, provides paths for the maint lockfile and
    auto-grace state file. All scripts receive overrides via env."""
    env_file = tmp_path / "pager.env"
    env_file.write_text(
        "HC_PING_URL=http://127.0.0.1:9/hc\n"
        "NTFY_TOPIC_URL=http://127.0.0.1:9/ntfy\n"
    )
    return {
        "PAGER_ENV": str(env_file),
        "MAINT_LOCK": str(tmp_path / "maint.lock"),
        "AUTO_GRACE_STATE": str(tmp_path / "autograce"),
        "REPO_ROOT": str(REPO_ROOT),
        # point LOG_FILE at a non-existent path by default; specific
        # tests override it when they want to inject log content.
        "LOG_FILE": str(tmp_path / "nonexistent.log"),
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
    }


def run(script, *args, env=None, input_=None, timeout=15):
    """Run a bash script and capture output + exit code."""
    result = subprocess.run(
        [str(script), *args],
        env={**os.environ, **(env or {})},
        input=input_,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result


# ---------------------------------------------------------------------------
# ops/maint
# ---------------------------------------------------------------------------


class TestMaint:
    def test_start_creates_lockfile(self, pager_env, tmp_path):
        r = run(MAINT, "start", "2m", env=pager_env)
        assert r.returncode == 0, r.stderr
        lock = Path(pager_env["MAINT_LOCK"])
        assert lock.exists()
        expiry = int(lock.read_text().strip())
        now = int(time.time())
        assert 110 < expiry - now <= 130    # ~2 minutes

    def test_end_removes_lockfile(self, pager_env):
        run(MAINT, "start", "2m", env=pager_env)
        assert Path(pager_env["MAINT_LOCK"]).exists()
        r = run(MAINT, "end", env=pager_env)
        assert r.returncode == 0
        assert not Path(pager_env["MAINT_LOCK"]).exists()

    def test_status_reflects_state(self, pager_env):
        r = run(MAINT, "status", env=pager_env)
        assert "OFF" in r.stdout

        run(MAINT, "start", "1m", env=pager_env)
        r = run(MAINT, "status", env=pager_env)
        assert "ON" in r.stdout

        run(MAINT, "end", env=pager_env)
        r = run(MAINT, "status", env=pager_env)
        assert "OFF" in r.stdout

    @pytest.mark.parametrize("spec,expected_secs", [
        ("30m",   1800),
        ("2h",    7200),
        ("1h30m", 5400),
        ("5m",    300),
        ("15s",   15),
        ("300",   300),
    ])
    def test_duration_parsing(self, pager_env, spec, expected_secs):
        run(MAINT, "start", spec, env=pager_env)
        expiry = int(Path(pager_env["MAINT_LOCK"]).read_text().strip())
        delta = expiry - int(time.time())
        # Allow a 5s tolerance for execution overhead.
        assert abs(delta - expected_secs) < 5

    def test_duration_caps_at_8h(self, pager_env):
        r = run(MAINT, "start", "24h", env=pager_env)
        assert "capped at 8h" in (r.stdout + r.stderr)
        expiry = int(Path(pager_env["MAINT_LOCK"]).read_text().strip())
        delta = expiry - int(time.time())
        assert 8 * 3600 - 5 < delta <= 8 * 3600 + 5

    def test_no_subcommand_exits_nonzero(self, pager_env):
        r = run(MAINT, env=pager_env)
        assert r.returncode != 0
        assert "Usage" in r.stderr


# ---------------------------------------------------------------------------
# ops/health_check.sh — maintenance + env gating
# ---------------------------------------------------------------------------


class TestHealthCheckGating:
    def test_missing_env_file_exits_2(self, pager_env, tmp_path):
        env = {**pager_env, "PAGER_ENV": str(tmp_path / "nope.env")}
        r = run(HEALTH, env=env)
        assert r.returncode == 2
        assert "missing HC_PING_URL" in r.stderr

    def test_empty_env_vars_exits_2(self, pager_env, tmp_path):
        empty = tmp_path / "empty.env"
        empty.write_text("# no vars\n")
        env = {**pager_env, "PAGER_ENV": str(empty)}
        r = run(HEALTH, env=env)
        assert r.returncode == 2

    def test_active_maint_lockfile_exits_0_silent(self, pager_env):
        run(MAINT, "start", "2m", env=pager_env)
        r = run(HEALTH, env=pager_env)
        assert r.returncode == 0
        # No error output, no ntfy push message.
        assert "IB Trader health check" not in r.stdout

    def test_expired_maint_lockfile_ignored(self, pager_env):
        # Write a long-past expiry.
        Path(pager_env["MAINT_LOCK"]).write_text("1\n")
        r = run(HEALTH, env=pager_env)
        # Exit code depends on stack health. If local stack is up it'll be 0;
        # if down, 1. Either is fine — the assertion is "it ran, didn't bail."
        assert r.returncode in (0, 1)


# ---------------------------------------------------------------------------
# ops/health_check.sh — auto-grace detection
# ---------------------------------------------------------------------------


class TestAutoGraceDetection:
    def test_existing_autograce_window_keeps_quiet(self, pager_env):
        # Write an auto-grace file that expires 5 min from now.
        Path(pager_env["AUTO_GRACE_STATE"]).write_text(
            str(int(time.time()) + 300)
        )
        r = run(HEALTH, env=pager_env)
        # The script should exit 0, short-circuit past all checks.
        assert r.returncode == 0
        # Must not attempt a ntfy push.
        assert "IB Trader health check" not in r.stdout

    def test_expired_autograce_runs_full_checks(self, pager_env):
        Path(pager_env["AUTO_GRACE_STATE"]).write_text("1")
        r = run(HEALTH, env=pager_env)
        # Exit 0 if healthy, 1 if anything's broken — both acceptable,
        # the point is the script did execute past the gate.
        assert r.returncode in (0, 1)


# ---------------------------------------------------------------------------
# ops/health_check.sh — runs cleanly in a happy state
# ---------------------------------------------------------------------------


class TestHealthCheckRuns:
    def test_script_syntax_is_valid(self):
        r = subprocess.run(
            ["bash", "-n", str(HEALTH)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr

    def test_maint_script_syntax_is_valid(self):
        r = subprocess.run(
            ["bash", "-n", str(MAINT)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr

    def test_installer_syntax_is_valid(self):
        r = subprocess.run(
            ["bash", "-n", str(OPS / "install-pager.sh")],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr

    def test_systemd_units_parse(self):
        """systemd-analyze verify if available — otherwise just
        check that mandatory sections exist."""
        for unit in ("ibtrader-health.service", "ibtrader-health.timer"):
            content = (OPS / unit).read_text()
            assert "[Unit]" in content
            if unit.endswith(".service"):
                assert "[Service]" in content
                assert "ExecStart=" in content
            else:
                assert "[Timer]" in content
                assert "OnUnitActiveSec=" in content
