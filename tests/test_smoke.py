"""Smoke tests: run every flag combination, check for errors and output formatting.

Uses a shared Drive service (session-scoped fixture) to avoid re-authing per test.
Tests that don't need Drive (errors, local delete, background) skip the fixture.
"""

import io
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

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
    parser.add_argument("--remove-job", action="store_true")
    parser.add_argument("--merge", nargs=2, default=None)
    parser.add_argument("--board", nargs="?", const="sync_board.html", default=None)
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

    def test_dry_run_does_not_delete_empty_local_conversations(self, tmp_path, monkeypatch):
        import sync_claude_history as sync

        fs_dir = tmp_path / "repo"
        fs_dir.mkdir()
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        encoded = str(fs_dir).replace("/", "-").replace("_", "-").replace(".", "-")
        project_dir = projects_dir / encoded
        project_dir.mkdir()
        empty_chat = project_dir / "empty.jsonl"
        empty_chat.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")

        monkeypatch.setattr(sync, "CLAUDE_PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(sync, "CODEX_HOME", tmp_path / ".codex")
        out = run_sync(["--push", "--dry-run"], service=None, root_folder_id=None)

        check_format(out)
        assert "Would clean 1 empty conversation(s)" in out
        assert empty_chat.exists()


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


class TestRemoveJob:
    def test_remove_by_repo(self, tmp_path):
        jobs = {
            "_daemon": {"pid": 99999},
            "flashinfer:all": {"repo": "flashinfer", "chat_id": None, "interval": 600},
            "sglang:all": {"repo": "sglang", "chat_id": None, "interval": 600},
        }
        jobs_file = tmp_path / ".sync_jobs.json"
        jobs_file.write_text(json.dumps(jobs))
        env = os.environ.copy()
        env["SYNC_STATE_DIR"] = str(tmp_path)
        r = subprocess.run(
            ["python", "sync_claude_history.py", "--remove-job", "--repo", "flashinfer"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
        assert "Removed [flashinfer:all]" in r.stdout
        assert "1 job(s) remaining" in r.stdout
        remaining = json.loads(jobs_file.read_text())
        assert "flashinfer:all" not in remaining
        assert "sglang:all" in remaining

    def test_remove_by_chat(self, tmp_path):
        jobs = {
            "_daemon": {},
            "all:abc12345-full-uuid": {"repo": None, "chat_id": "abc12345-full-uuid", "interval": 600},
        }
        jobs_file = tmp_path / ".sync_jobs.json"
        jobs_file.write_text(json.dumps(jobs))
        env = os.environ.copy()
        env["SYNC_STATE_DIR"] = str(tmp_path)
        r = subprocess.run(
            ["python", "sync_claude_history.py", "--remove-job", "--chat", "abc12345"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
        assert "Removed" in r.stdout
        assert "No jobs remaining" in r.stdout
        assert not jobs_file.exists()

    def test_remove_no_match(self, tmp_path):
        jobs = {"_daemon": {}, "sglang:all": {"repo": "sglang", "chat_id": None, "interval": 600}}
        jobs_file = tmp_path / ".sync_jobs.json"
        jobs_file.write_text(json.dumps(jobs))
        env = os.environ.copy()
        env["SYNC_STATE_DIR"] = str(tmp_path)
        r = subprocess.run(
            ["python", "sync_claude_history.py", "--remove-job", "--repo", "nonexistent"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        assert r.returncode == 0
        assert "No matching" in r.stdout


class TestErrors:
    def test_delete_requires_filter(self):
        _run_expect_fail(["-d"])

    def test_local_requires_delete(self):
        _run_expect_fail(["--local"])

    def test_bad_background_interval(self):
        _run_expect_fail(["--background", "0"])

    def test_remove_job_requires_filter(self):
        _run_expect_fail(["--remove-job"])


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


class TestCodexSupport:
    class _FakeRequest:
        def __init__(self, fn):
            self._fn = fn

        def execute(self):
            return self._fn()

    class _FakeMedia:
        def __init__(self, filename, *args, **kwargs):
            self.filename = filename

    class _FakeFiles:
        def __init__(self, drive):
            self.drive = drive

        def create(self, body=None, media_body=None, fields=None):
            return TestCodexSupport._FakeRequest(
                lambda: self.drive.create(body or {}, media_body)
            )

        def update(self, fileId, body=None, media_body=None):
            return TestCodexSupport._FakeRequest(
                lambda: self.drive.update(fileId, body or {}, media_body)
            )

    class _FakeDrive:
        def __init__(self):
            self.root_id = "root"
            self._next_id = 1
            self.folders = {
                self.root_id: {
                    "id": self.root_id,
                    "name": "root",
                    "parent": None,
                    "description": "",
                    "folders": {},
                    "files": {},
                }
            }
            self.files_by_id = {}

        def files(self):
            return TestCodexSupport._FakeFiles(self)

        def _id(self, prefix):
            value = f"{prefix}{self._next_id}"
            self._next_id += 1
            return value

        def get_or_create_folder(self, service, folder_name, parent_id=None):
            parent_id = parent_id or self.root_id
            existing = self.folders[parent_id]["folders"].get(folder_name)
            if existing:
                return existing
            folder_id = self._id("folder")
            self.folders[parent_id]["folders"][folder_name] = folder_id
            self.folders[folder_id] = {
                "id": folder_id,
                "name": folder_name,
                "parent": parent_id,
                "description": "",
                "folders": {},
                "files": {},
            }
            return folder_id

        def list_drive_folders(self, service, parent_id, include_description=False):
            out = {}
            for name, folder_id in self.folders[parent_id]["folders"].items():
                if include_description:
                    out[name] = {
                        "id": folder_id,
                        "description": self.folders[folder_id].get("description", ""),
                    }
                else:
                    out[name] = folder_id
            return out

        def list_remote_files(self, service, folder_id):
            return {
                name: {
                    "id": f["id"],
                    "name": name,
                    "modifiedTime": f["modifiedTime"],
                    "md5Checksum": f["md5Checksum"],
                    "size": str(f["size"]),
                }
                for name, f in self.folders[folder_id]["files"].items()
            }

        def put_file(self, folder_id, name, content, existing_id=None, modified_time=None):
            if isinstance(content, str):
                content = content.encode()
            file_id = existing_id or self.folders[folder_id]["files"].get(name, {}).get("id") or self._id("file")
            if modified_time is None:
                modified_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            record = {
                "id": file_id,
                "name": name,
                "parent": folder_id,
                "content": content,
                "modifiedTime": modified_time,
                "md5Checksum": hashlib.md5(content).hexdigest(),
                "size": len(content),
            }
            self.folders[folder_id]["files"][name] = record
            self.files_by_id[file_id] = record
            return {"id": file_id}

        def create(self, body, media_body):
            if body.get("mimeType") == "application/vnd.google-apps.folder":
                return {"id": self.get_or_create_folder(None, body["name"], body.get("parents", [self.root_id])[0])}
            parent_id = body["parents"][0]
            with open(media_body.filename, "rb") as f:
                content = f.read()
            return self.put_file(parent_id, body["name"], content)

        def update(self, file_id, body, media_body):
            if file_id in self.folders:
                self.folders[file_id].update(body)
                return {"id": file_id}
            record = self.files_by_id[file_id]
            if media_body is not None:
                with open(media_body.filename, "rb") as f:
                    return self.put_file(record["parent"], record["name"], f.read(), existing_id=file_id)
            record.update(body)
            return {"id": file_id}

        def download(self, file_id, local_path):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(self.files_by_id[file_id]["content"])

    def _git_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
            cwd=repo, check=True, capture_output=True,
        )
        return repo

    def _patch_fake_drive(self, monkeypatch, sync):
        drive = self._FakeDrive()
        monkeypatch.setattr(sync, "MediaFileUpload", self._FakeMedia)
        monkeypatch.setattr(sync, "get_or_create_folder", drive.get_or_create_folder)
        monkeypatch.setattr(sync, "list_drive_folders", drive.list_drive_folders)
        monkeypatch.setattr(sync, "list_remote_files", drive.list_remote_files)
        monkeypatch.setattr(
            sync,
            "batch_list_drive_folders",
            lambda service, folder_ids: {
                key: drive.list_drive_folders(service, folder_id)
                for key, folder_id in folder_ids.items()
            },
        )
        monkeypatch.setattr(
            sync,
            "batch_list_remote_files",
            lambda service, folder_ids: {
                key: drive.list_remote_files(service, folder_id)
                for key, folder_id in folder_ids.items()
            },
        )
        monkeypatch.setattr(
            sync,
            "upload_string",
            lambda service, content, name, folder_id, existing_id=None: drive.put_file(
                folder_id, name, content, existing_id=existing_id
            ),
        )
        monkeypatch.setattr(
            sync,
            "download_file",
            lambda service, file_id, local_path: drive.download(file_id, local_path),
        )
        monkeypatch.setattr(
            sync,
            "download_string",
            lambda service, file_id: drive.files_by_id[file_id]["content"].decode(),
        )
        return drive

    def _codex_rollout(self, codex_home, repo, session_id="019e-test"):
        sessions = codex_home / "sessions" / "2026" / "06" / "05"
        sessions.mkdir(parents=True)
        subdir = repo / "src"
        subdir.mkdir()
        p = sessions / f"rollout-2026-06-05T01-02-03-{session_id}.jsonl"
        rows = [
            {
                "timestamp": "2026-06-05T01:02:03Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(subdir),
                    "git": {"repository_url": "git@github.com:org/repo.git"},
                },
            },
            {
                "timestamp": "2026-06-05T01:02:04Z",
                "type": "turn_context",
                "payload": {"cwd": str(subdir)},
            },
            {
                "timestamp": "2026-06-05T01:02:05Z",
                "type": "response_item",
                "payload": {
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first prompt"}],
                    }
                },
            },
        ]
        with open(p, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return p

    def test_codex_index_and_cwd_rewrite(self, tmp_path):
        from sync_claude_history import (
            build_codex_index,
            normalize_git_url,
            parse_codex_rollout,
            rewrite_codex_cwd_if_needed,
        )

        repo = self._git_repo(tmp_path)
        codex_home = tmp_path / ".codex"
        rollout = self._codex_rollout(codex_home, repo)

        meta = parse_codex_rollout(rollout)
        assert meta["session_id"] == "019e-test"
        assert meta["first_user_message"] == "first prompt"

        index = build_codex_index(codex_home)
        key = normalize_git_url("git@github.com:org/repo.git")
        assert key in index
        assert index[key][0]["rel_path"] == "src"

        assert rewrite_codex_cwd_if_needed(rollout, "/new/repo/src") == 2
        after = [json.loads(line) for line in rollout.read_text().splitlines()]
        assert after[0]["payload"]["cwd"] == "/new/repo/src"
        assert after[1]["payload"]["cwd"] == "/new/repo/src"

    def test_codex_subfolder_roundtrip_and_session_index(self, tmp_path):
        from sync_claude_history import (
            codex_drive_subfolder_to_rel_path,
            codex_rel_path_to_drive_subfolder,
            upsert_codex_session_index,
        )

        sf = codex_rel_path_to_drive_subfolder("src/tools")
        assert sf == "src__tools"
        assert codex_drive_subfolder_to_rel_path(sf) == "src/tools"
        assert codex_drive_subfolder_to_rel_path("_codex__root") == "."

        codex_home = tmp_path / ".codex"
        upsert_codex_session_index(
            [{"session_id": "sid-1", "title": "pulled chat", "updated_at": 1780500000}],
            codex_home,
        )
        rows = [json.loads(line) for line in (codex_home / "session_index.jsonl").read_text().splitlines()]
        assert rows == [{
            "id": "sid-1",
            "thread_name": "pulled chat",
            "updated_at": "2026-06-03T15:20:00Z",
        }]

    def test_board_shows_claude_and_codex_prefixes(self, tmp_path, monkeypatch):
        import sync_claude_history as sync

        repo = self._git_repo(tmp_path)
        codex_home = tmp_path / ".codex"
        self._codex_rollout(codex_home, repo, session_id="019e-board")
        monkeypatch.setattr(sync, "CODEX_HOME", codex_home)

        claude_project = tmp_path / "claude-project"
        claude_project.mkdir()
        claude_file = claude_project / "claude-session.jsonl"
        claude_rows = [
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": "ok"}},
            {"type": "custom-title", "customTitle": "claude title"},
        ]
        with open(claude_file, "w") as f:
            for row in claude_rows:
                f.write(json.dumps(row) + "\n")

        key = sync.normalize_git_url("git@github.com:org/repo.git")
        monkeypatch.setattr(
            sync,
            "build_local_index",
            lambda: {key: [(claude_project, "git@github.com:org/repo.git", str(repo), ".")]},
        )

        board = sync.generate_board(tmp_path / "board.html")
        text = board.read_text()
        assert "[claude]" in text
        assert "[codex]" in text
        assert "claude title" in text
        assert "first prompt" in text

    def test_codex_run_sync_pushes_rollout_to_drive_subfolder(self, tmp_path, monkeypatch):
        import sync_claude_history as sync

        repo = self._git_repo(tmp_path)
        codex_home = tmp_path / ".codex"
        rollout = self._codex_rollout(codex_home, repo, session_id="019e-push")
        monkeypatch.setattr(sync, "CODEX_HOME", codex_home)
        monkeypatch.setattr(sync, "CLAUDE_PROJECTS_DIR", tmp_path / "empty-claude")
        drive = self._patch_fake_drive(monkeypatch, sync)

        out = run_sync(["--push"], drive, drive.root_id)
        check_format(out)
        assert "[codex] git@github.com:org/repo.git" in out
        assert "codex 1 pushed, 0 pulled, 0 unchanged" in out
        assert '[codex group] "first prompt"' in out

        url_key = sync.normalize_git_url("git@github.com:org/repo.git")
        repo_folder_id = drive.folders[drive.root_id]["folders"][url_key]
        assert drive.folders[repo_folder_id]["description"] == "git@github.com:org/repo.git"
        metadata = json.loads(drive.folders[repo_folder_id]["files"]["_metadata.json"]["content"])
        assert metadata["sources"] == ["claude", "codex"]

        codex_folder_id = drive.folders[repo_folder_id]["folders"]["src"]
        remote_files = drive.folders[codex_folder_id]["files"]
        remote_name = f"_codex__{rollout.name}"
        assert remote_name in remote_files
        assert json.loads(remote_files[remote_name]["content"].splitlines()[0])["payload"]["id"] == "019e-push"

    def test_codex_run_sync_pulls_remote_rollout_and_indexes_it(self, tmp_path, monkeypatch):
        import sync_claude_history as sync

        repo = self._git_repo(tmp_path)
        (repo / "src").mkdir()
        codex_home = tmp_path / ".codex"
        monkeypatch.setattr(sync, "CODEX_HOME", codex_home)
        monkeypatch.setattr(sync, "CLAUDE_PROJECTS_DIR", tmp_path / "empty-claude")
        drive = self._patch_fake_drive(monkeypatch, sync)

        raw_url = "git@github.com:org/repo.git"
        url_key = sync.normalize_git_url(raw_url)
        repo_folder_id = drive.get_or_create_folder(None, url_key, drive.root_id)
        drive.folders[repo_folder_id]["description"] = raw_url
        codex_folder_id = drive.get_or_create_folder(None, "src", repo_folder_id)
        fname = "rollout-2026-06-05T02-03-04-019e-pull.jsonl"
        remote_name = f"_codex__{fname}"
        old_cwd = "/old/machine/repo/src"
        rows = [
            {
                "timestamp": "2026-06-05T02:03:04Z",
                "type": "session_meta",
                "payload": {"id": "019e-pull", "cwd": old_cwd, "git": {"repository_url": raw_url}},
            },
            {
                "timestamp": "2026-06-05T02:03:05Z",
                "type": "turn_context",
                "payload": {"cwd": old_cwd},
            },
            {
                "timestamp": "2026-06-05T02:03:06Z",
                "type": "response_item",
                "payload": {
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "remote prompt"}],
                    }
                },
            },
        ]
        drive.put_file(
            codex_folder_id,
            remote_name,
            "".join(json.dumps(row) + "\n" for row in rows),
            modified_time="2026-06-05T02:03:06Z",
        )
        monkeypatch.setattr(sync, "scan_local_git_repos", lambda: {url_key: (str(repo), raw_url)})

        out = run_sync(["--pull"], drive, drive.root_id)
        check_format(out)
        assert "codex 0 pushed, 1 pulled, 0 unchanged" in out

        local_path = codex_home / "sessions" / "2026" / "06" / "05" / fname
        pulled = [json.loads(line) for line in local_path.read_text().splitlines()]
        assert pulled[0]["payload"]["cwd"] == str(repo / "src")
        assert pulled[1]["payload"]["cwd"] == str(repo / "src")

        index_rows = [
            json.loads(line)
            for line in (codex_home / "session_index.jsonl").read_text().splitlines()
        ]
        assert index_rows == [{
            "id": "019e-pull",
            "thread_name": "remote prompt",
            "updated_at": "2026-06-05T02:03:06Z",
        }]

    def test_codex_run_sync_pulls_legacy_codex_subfolder(self, tmp_path, monkeypatch):
        import sync_claude_history as sync

        repo = self._git_repo(tmp_path)
        (repo / "src").mkdir()
        codex_home = tmp_path / ".codex"
        monkeypatch.setattr(sync, "CODEX_HOME", codex_home)
        monkeypatch.setattr(sync, "CLAUDE_PROJECTS_DIR", tmp_path / "empty-claude")
        drive = self._patch_fake_drive(monkeypatch, sync)

        raw_url = "git@github.com:org/repo.git"
        url_key = sync.normalize_git_url(raw_url)
        repo_folder_id = drive.get_or_create_folder(None, url_key, drive.root_id)
        drive.folders[repo_folder_id]["description"] = raw_url
        legacy_folder_id = drive.get_or_create_folder(None, "_codex__src", repo_folder_id)
        fname = "rollout-2026-06-05T02-03-04-019e-legacy.jsonl"
        old_cwd = "/old/machine/repo/src"
        rows = [
            {
                "timestamp": "2026-06-05T02:03:04Z",
                "type": "session_meta",
                "payload": {"id": "019e-legacy", "cwd": old_cwd, "git": {"repository_url": raw_url}},
            },
            {
                "timestamp": "2026-06-05T02:03:06Z",
                "type": "response_item",
                "payload": {
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "legacy prompt"}],
                    }
                },
            },
        ]
        drive.put_file(
            legacy_folder_id,
            fname,
            "".join(json.dumps(row) + "\n" for row in rows),
            modified_time="2026-06-05T02:03:06Z",
        )
        monkeypatch.setattr(sync, "scan_local_git_repos", lambda: {url_key: (str(repo), raw_url)})

        out = run_sync(["--pull"], drive, drive.root_id)
        check_format(out)
        assert "codex 0 pushed, 1 pulled, 0 unchanged" in out
        local_path = codex_home / "sessions" / "2026" / "06" / "05" / fname
        assert local_path.exists()
        pulled = [json.loads(line) for line in local_path.read_text().splitlines()]
        assert pulled[0]["payload"]["cwd"] == str(repo / "src")

    def test_repair_uuid_chain(self, tmp_path):
        """repair_uuid_chain bridges broken parentUuid links."""
        from sync_claude_history import repair_uuid_chain

        p = tmp_path / "conv.jsonl"
        entries = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"role": "user", "content": "start"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"role": "assistant", "content": "ok"}},
            {"type": "system", "uuid": "s1", "parentUuid": "a1"},
            {"type": "queue-operation"},
            # Break: parent points to uuid that was never written
            {"type": "system", "uuid": "s2", "parentUuid": "MISSING-UUID"},
            {"type": "user", "uuid": "u2", "parentUuid": "s2",
             "message": {"role": "user", "content": "next"}},
            {"type": "assistant", "uuid": "a2", "parentUuid": "u2",
             "message": {"role": "assistant", "content": "done"}},
        ]
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        old_mtime = p.stat().st_mtime
        n_fixes = repair_uuid_chain(p)
        assert n_fixes == 1

        with open(p) as f:
            after = [json.loads(l) for l in f if l.strip()]
        # s2 should now point to s1 (nearest preceding uuid'd line)
        s2 = [e for e in after if e.get("uuid") == "s2"][0]
        assert s2["parentUuid"] == "s1"
        # chain should be fully walkable: a2 -> u2 -> s2 -> s1 -> a1 -> u1
        uuid_map = {e["uuid"]: e for e in after if e.get("uuid")}
        current = "a2"
        chain = [current]
        while uuid_map[current].get("parentUuid"):
            current = uuid_map[current]["parentUuid"]
            chain.append(current)
        assert chain == ["a2", "u2", "s2", "s1", "a1", "u1"]

        # mtime preserved
        assert abs(p.stat().st_mtime - old_mtime) < 1

    def test_repair_no_breaks(self, tmp_path):
        """repair_uuid_chain returns 0 on an intact chain."""
        from sync_claude_history import repair_uuid_chain

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

        assert repair_uuid_chain(p) == 0

    def test_repair_multiple_breaks(self, tmp_path):
        """repair_uuid_chain fixes multiple breaks in one pass."""
        from sync_claude_history import repair_uuid_chain

        p = tmp_path / "conv.jsonl"
        entries = [
            {"type": "user", "uuid": "u1", "parentUuid": None},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1"},
            {"type": "system", "uuid": "s1", "parentUuid": "MISSING1"},
            {"type": "user", "uuid": "u2", "parentUuid": "s1"},
            {"type": "assistant", "uuid": "a2", "parentUuid": "u2"},
            {"type": "system", "uuid": "s2", "parentUuid": "MISSING2"},
            {"type": "user", "uuid": "u3", "parentUuid": "s2"},
        ]
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        n_fixes = repair_uuid_chain(p)
        assert n_fixes == 2

        with open(p) as f:
            after = [json.loads(l) for l in f if l.strip()]
        uuid_map = {e["uuid"]: e for e in after if e.get("uuid")}
        assert uuid_map["s1"]["parentUuid"] == "a1"
        assert uuid_map["s2"]["parentUuid"] == "a2"

    def test_repair_dedup_orphan_branches(self, tmp_path):
        """repair_uuid_chain removes duplicate uuid entries from orphan branches."""
        from sync_claude_history import repair_uuid_chain

        p = tmp_path / "conv.jsonl"
        entries = [
            # Orphan branch (old compacted start, same uuid as line 4)
            {"type": "user", "uuid": "u1", "parentUuid": "DEAD",
             "message": {"role": "user", "content": "old summary"}},
            {"type": "assistant", "uuid": "a1-old", "parentUuid": "u1",
             "message": {"role": "assistant", "content": "old reply"}},
            # Metadata (no uuid)
            {"type": "queue-operation"},
            # Main branch
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"role": "user", "content": "real start"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"role": "assistant", "content": "real reply"}},
            {"type": "user", "uuid": "u2", "parentUuid": "a1",
             "message": {"role": "user", "content": "continue"}},
            # Trailing metadata
            {"type": "custom-title"},
        ]
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        n_fixes = repair_uuid_chain(p)
        assert n_fixes > 0

        with open(p) as f:
            after = [json.loads(l) for l in f if l.strip()]
        # Orphan lines (old u1 and a1-old) should be removed
        uuids = [e.get("uuid") for e in after if e.get("uuid")]
        assert uuids == ["u1", "a1", "u2"]
        # The remaining u1 should be the main branch one
        u1 = [e for e in after if e.get("uuid") == "u1"][0]
        assert u1["parentUuid"] is None
        assert u1["message"]["content"] == "real start"
        # Trailing metadata (custom-title) should be kept
        assert any(e.get("type") == "custom-title" for e in after)

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


# ---------------------------------------------------------------------------
# Merge — conflict resolution for multi-machine background sync
# ---------------------------------------------------------------------------

class TestMerge:
    def test_merge_both_sides_unique(self, tmp_path):
        """Merges unique entries from both local and remote, ordered by timestamp."""
        from sync_claude_history import merge_jsonl_by_timestamp

        # Common base: u1, a1
        common = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "timestamp": "2026-04-21T01:00:00Z",
             "message": {"role": "user", "content": "start"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": "2026-04-21T01:01:00Z",
             "message": {"role": "assistant", "content": "ok"}},
        ]
        # Local added u2 at T=03
        local_entries = common + [
            {"type": "user", "uuid": "u2", "parentUuid": "a1",
             "timestamp": "2026-04-21T01:03:00Z",
             "message": {"role": "user", "content": "local msg"}},
        ]
        # Remote added u3 at T=02
        remote_entries = common + [
            {"type": "user", "uuid": "u3", "parentUuid": "a1",
             "timestamp": "2026-04-21T01:02:00Z",
             "message": {"role": "user", "content": "remote msg"}},
        ]

        p = tmp_path / "conv.jsonl"
        with open(p, "w") as f:
            for e in local_entries:
                f.write(json.dumps(e) + "\n")

        remote_text = "\n".join(json.dumps(e) for e in remote_entries) + "\n"
        result = merge_jsonl_by_timestamp(p, remote_text)
        assert result is True

        with open(p) as f:
            merged = [json.loads(l) for l in f if l.strip()]

        uuids = [e["uuid"] for e in merged if e.get("uuid")]
        # u3 (T=02) should come before u2 (T=03)
        assert uuids == ["u1", "a1", "u3", "u2"]

    def test_merge_no_conflict(self, tmp_path):
        """Returns False when one side is a superset (no merge needed)."""
        from sync_claude_history import merge_jsonl_by_timestamp

        entries = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "timestamp": "2026-04-21T01:00:00Z"},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": "2026-04-21T01:01:00Z"},
        ]
        # Local has both, remote has only u1 (local is superset)
        p = tmp_path / "conv.jsonl"
        with open(p, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        remote_text = json.dumps(entries[0]) + "\n"
        assert merge_jsonl_by_timestamp(p, remote_text) is False

    def test_merge_preserves_metadata(self, tmp_path):
        """Metadata entries (no uuid) from both sides are kept."""
        from sync_claude_history import merge_jsonl_by_timestamp

        common = [{"type": "user", "uuid": "u1", "parentUuid": None,
                    "timestamp": "2026-04-21T01:00:00Z"}]
        local_entries = common + [
            {"type": "user", "uuid": "u2", "parentUuid": "u1",
             "timestamp": "2026-04-21T01:02:00Z"},
            {"type": "custom-title"},
        ]
        remote_entries = common + [
            {"type": "user", "uuid": "u3", "parentUuid": "u1",
             "timestamp": "2026-04-21T01:01:00Z"},
            {"type": "file-history-snapshot"},
        ]

        p = tmp_path / "conv.jsonl"
        with open(p, "w") as f:
            for e in local_entries:
                f.write(json.dumps(e) + "\n")

        remote_text = "\n".join(json.dumps(e) for e in remote_entries) + "\n"
        assert merge_jsonl_by_timestamp(p, remote_text) is True

        with open(p) as f:
            merged = [json.loads(l) for l in f if l.strip()]

        types = [e.get("type") for e in merged]
        assert "custom-title" in types
        assert "file-history-snapshot" in types

    def test_merge_chain_intact(self, tmp_path):
        """After merge, parentUuid chain is walkable from tail to root."""
        from sync_claude_history import merge_jsonl_by_timestamp

        common = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "timestamp": "2026-04-21T01:00:00Z"},
        ]
        local_entries = common + [
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": "2026-04-21T01:03:00Z"},
            {"type": "user", "uuid": "u2", "parentUuid": "a1",
             "timestamp": "2026-04-21T01:05:00Z"},
        ]
        remote_entries = common + [
            {"type": "user", "uuid": "r1", "parentUuid": "u1",
             "timestamp": "2026-04-21T01:02:00Z"},
            {"type": "assistant", "uuid": "r2", "parentUuid": "r1",
             "timestamp": "2026-04-21T01:04:00Z"},
        ]

        p = tmp_path / "conv.jsonl"
        with open(p, "w") as f:
            for e in local_entries:
                f.write(json.dumps(e) + "\n")

        remote_text = "\n".join(json.dumps(e) for e in remote_entries) + "\n"
        assert merge_jsonl_by_timestamp(p, remote_text) is True

        with open(p) as f:
            merged = [json.loads(l) for l in f if l.strip()]

        uuid_entries = {e["uuid"]: e for e in merged if e.get("uuid")}
        # Walk from last entry to root
        uuids_ordered = [e["uuid"] for e in merged if e.get("uuid")]
        current = uuids_ordered[-1]
        chain = [current]
        while uuid_entries[current].get("parentUuid"):
            parent = uuid_entries[current]["parentUuid"]
            assert parent in uuid_entries, f"Dangling parent {parent}"
            current = parent
            chain.append(current)
        chain.reverse()
        assert chain == uuids_ordered
