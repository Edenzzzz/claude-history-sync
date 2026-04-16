"""Smoke tests: run every flag combination, check for errors and output formatting.

Uses a shared Drive service (session-scoped fixture) to avoid re-authing per test.
Tests that don't need Drive (errors, local delete, background) skip the fixture.
"""

import io
import os
import re
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BORDER = "╠═══"


@pytest.fixture(scope="session")
def drive():
    """Shared Drive service + root folder ID, authed once for all tests."""
    from sync_claude_history import patch_dns_if_needed, get_drive_service, get_or_create_folder, DRIVE_FOLDER_NAME
    patch_dns_if_needed()
    service = get_drive_service()
    root_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
    return service, root_id


def _parse_args(args_list):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--pull", dest="pull_only", action="store_true")
    parser.add_argument("--push", dest="push_only", action="store_true")
    parser.add_argument("-d", "--delete", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--repo", type=str, default=None)
    parser.add_argument("--chat", type=str, default=None, dest="chat_id")
    parser.add_argument("--background", type=int, nargs="?", const=600, default=None)
    parser.add_argument("--merge", nargs=2, default=None)
    return parser.parse_args(args_list)


def run_sync(args_list, service, root_folder_id):
    """Run sync in-process with shared Drive service. Returns captured stdout."""
    from sync_claude_history import run_sync as _run_sync
    args = _parse_args(args_list)
    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        _run_sync(args, service, root_folder_id)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def check_format(output):
    """Verify output formatting is not corrupted."""
    assert "Traceback" not in output, f"Traceback in output:\n{output[-500:]}"
    for line in output.strip().splitlines():
        assert line.count(BORDER) <= 1, f"Doubled border: {line}"


# ---------------------------------------------------------------------------
# Sync flags (share Drive service)
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_basic(self, drive):
        out = run_sync(["--dry-run"], *drive)
        check_format(out)
        assert "Found" in out
        assert "Done." in out

    def test_verbose(self, drive):
        out = run_sync(["--dry-run", "-v"], *drive)
        check_format(out)
        assert "Found" in out

    def test_push(self, drive):
        out = run_sync(["--push", "--dry-run"], *drive)
        check_format(out)

    def test_pull(self, drive):
        out = run_sync(["--pull", "--dry-run"], *drive)
        check_format(out)


class TestFilters:
    def test_repo_filter(self, drive):
        out = run_sync(["--dry-run", "--repo", "sglang"], *drive)
        check_format(out)

    def test_chat_filter(self, drive):
        out = run_sync(["--dry-run", "--chat", "df9a6a22"], *drive)
        check_format(out)

    def test_repo_and_chat(self, drive):
        out = run_sync(["--dry-run", "--repo", "sglang", "--chat", "de1128"], *drive)
        check_format(out)


class TestDelete:
    def test_delete_dry(self, drive):
        out = run_sync(["-d", "--repo", "sglang", "--dry-run"], *drive)
        check_format(out)

    def test_delete_chat_dry(self, drive):
        out = run_sync(["-d", "--repo", "sglang", "--chat", "de1128", "--dry-run"], *drive)
        check_format(out)

    def test_delete_local_dry(self):
        from sync_claude_history import run_sync as _run_sync
        args = _parse_args(["-d", "--local", "--repo", "sglang", "--dry-run"])
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()
        try:
            _run_sync(args, service=None, root_folder_id=None)
        finally:
            sys.stdout = old_stdout
        check_format(buf.getvalue())


# ---------------------------------------------------------------------------
# Background (subprocess — needs to fork)
# ---------------------------------------------------------------------------

class TestBackground:
    def test_start_and_stop(self, tmp_path):
        env = os.environ.copy()
        env["SYNC_STATE_DIR"] = str(tmp_path)
        r = subprocess.run(
            ["python", "sync_claude_history.py", "--background", "600", "--chat", "df9a6a22"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
        check_format(r.stdout)
        assert "PID:" in r.stdout

        pid_match = re.search(r"PID:\s*(\d+)", r.stdout)
        assert pid_match
        pid = int(pid_match.group(1))
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# Error handling — should exit cleanly, no tracebacks
# ---------------------------------------------------------------------------

def _run_expect_fail(args):
    r = subprocess.run(
        ["python", "sync_claude_history.py"] + args,
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "Traceback" not in r.stdout
    return r


class TestErrors:
    def test_delete_requires_filter(self):
        _run_expect_fail(["-d"])

    def test_local_requires_delete(self):
        _run_expect_fail(["--local"])

    def test_bad_background_interval(self):
        _run_expect_fail(["--background", "0"])


# ---------------------------------------------------------------------------
# Trim compact — unit tests for the helper (invoked automatically by sync)
# ---------------------------------------------------------------------------

class TestTrimCompact:
    def test_trim_function(self, tmp_path):
        """Unit test the trim helper on a synthetic conversation."""
        import json
        from sync_claude_history import trim_at_last_compact

        p = tmp_path / "conv.jsonl"
        # 3 pre-compact entries, a /compact marker, a compact summary, 2 post entries
        entries = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"role": "assistant", "content": "hello"}},
            {"type": "user", "uuid": "u2", "parentUuid": "a1",
             "message": {"role": "user", "content": "/compact"}},
            {"type": "user", "uuid": "c1", "parentUuid": "u2",
             "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
             "message": {"role": "user", "content": "This session is being continued…"}},
            {"type": "user", "uuid": "u3", "parentUuid": "c1",
             "message": {"role": "user", "content": "next"}},
            {"type": "assistant", "uuid": "a2", "parentUuid": "u3",
             "message": {"role": "assistant", "content": "ok"}},
        ]
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        before_size = p.stat().st_size
        trimmed, before, after = trim_at_last_compact(p)
        assert trimmed is True
        assert before == before_size
        assert after < before

        with open(p) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) == 3
        assert lines[0].get("isCompactSummary") is True
        assert lines[0]["parentUuid"] == "u2"  # original parent preserved (dangling OK)
        assert lines[0]["uuid"] == "c1"
        assert lines[1]["uuid"] == "u3"
        assert lines[1]["parentUuid"] == "c1"  # chain intact
        assert lines[2]["uuid"] == "a2"

        # Backup file should exist
        assert p.with_suffix(".jsonl.pretrim.bak").exists()

    def test_preserve_mtime(self, tmp_path):
        """Trim should preserve the original mtime so sync direction isn't flipped."""
        import json
        import os
        import time
        from sync_claude_history import trim_at_last_compact

        p = tmp_path / "conv.jsonl"
        entries = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"role": "user", "content": "hi"}},
            {"type": "user", "uuid": "c1", "parentUuid": "u1",
             "isCompactSummary": True,
             "message": {"role": "user", "content": "summary"}},
            {"type": "user", "uuid": "u2", "parentUuid": "c1",
             "message": {"role": "user", "content": "after"}},
        ]
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        # Stamp an old mtime
        old_mtime = time.time() - 86400
        os.utime(p, (old_mtime, old_mtime))

        trimmed, _, _ = trim_at_last_compact(p)
        assert trimmed is True
        new_mtime = p.stat().st_mtime
        assert abs(new_mtime - old_mtime) < 1, \
            f"mtime changed: was {old_mtime}, now {new_mtime}"

    def test_idempotent(self, tmp_path):
        """Re-trimming an already-trimmed file should be a no-op."""
        import json
        from sync_claude_history import trim_at_last_compact

        p = tmp_path / "conv.jsonl"
        entries = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"role": "user", "content": "hi"}},
            {"type": "user", "uuid": "c1", "parentUuid": "u1",
             "isCompactSummary": True,
             "message": {"role": "user", "content": "summary"}},
            {"type": "user", "uuid": "u2", "parentUuid": "c1",
             "message": {"role": "user", "content": "after"}},
        ]
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        # First trim should reduce
        trimmed1, _, _ = trim_at_last_compact(p)
        assert trimmed1 is True
        size_after_first = p.stat().st_size

        # Second trim should be a no-op
        trimmed2, b2, a2 = trim_at_last_compact(p)
        assert trimmed2 is False
        assert b2 == a2 == size_after_first

    def test_cwd_rewrite(self, tmp_path):
        """rewrite_cwd_if_needed updates all cwd fields and preserves mtime."""
        import json
        import os
        import time
        from sync_claude_history import rewrite_cwd_if_needed

        p = tmp_path / "conv.jsonl"
        entries = [
            {"type": "user", "uuid": "u1", "cwd": "/orig/repo",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "uuid": "a1", "cwd": "/orig/repo",
             "message": {"role": "assistant", "content": "hello"}},
            # Entry without cwd — should be left untouched
            {"type": "summary", "summary": "a title"},
            {"type": "user", "uuid": "u2", "cwd": "/orig/repo",
             "message": {"role": "user", "content": "more"}},
        ]
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        old_mtime = time.time() - 12345
        os.utime(p, (old_mtime, old_mtime))

        n = rewrite_cwd_if_needed(p, "/local/repo")
        assert n == 3

        with open(p) as f:
            after = [json.loads(l) for l in f if l.strip()]
        assert after[0]["cwd"] == "/local/repo"
        assert after[1]["cwd"] == "/local/repo"
        assert "cwd" not in after[2]  # summary entry unchanged
        assert after[3]["cwd"] == "/local/repo"

        # mtime preserved
        assert abs(p.stat().st_mtime - old_mtime) < 1

        # Second call is a no-op (returns 0)
        assert rewrite_cwd_if_needed(p, "/local/repo") == 0

    def test_trim_no_compact(self, tmp_path):
        """File without a compact summary is left untouched."""
        import json
        from sync_claude_history import trim_at_last_compact

        p = tmp_path / "conv.jsonl"
        entries = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"role": "assistant", "content": "hello"}},
        ]
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        before_size = p.stat().st_size
        trimmed, before, after = trim_at_last_compact(p)
        assert trimmed is False
        assert before == after == before_size
        assert not p.with_suffix(".jsonl.pretrim.bak").exists()
