#!/usr/bin/env python3
"""
Sync Claude Code conversation history across machines via Google Drive.

Drive folder structure (organized by normalized git remote, not local path):
  claude-code-history/
    github.com__flashinfer-ai__flashinfer/    # normalized remote URL
      _metadata.json                           # {remote_url, local_paths: [...]}
      abc123.jsonl
      def456.jsonl
    github.com__NVIDIA__cutlass/
      ...

On push: resolves each local project dir to its git remote, uploads under that key.
On pull: for each remote folder, finds the local project dir whose repo matches,
         downloads into it. Skips repos not cloned locally.

Setup:
  1. Enable Google Drive API, create OAuth credentials (desktop app)
  2. pip install google-auth google-auth-oauthlib google-api-python-client
  3. Place credentials.json in this repo (gitignored)
  4. First run will open browser for OAuth consent

Usage:
  python sync_claude_history.py          # bidirectional sync
  python sync_claude_history.py --pull   # only download newer remote files
  python sync_claude_history.py --push   # only upload newer local files
  python sync_claude_history.py --dry-run
"""

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

GOOGLE_API_HOSTS = ["oauth2.googleapis.com", "www.googleapis.com"]


def _check_reachable(host, port=443, timeout=3):
    """Check if a host:port is reachable via TCP."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (OSError, socket.timeout):
        return False


def _resolve_via_doh(hostname):
    """Resolve a hostname using Google's DNS-over-HTTPS, bypassing local DNS."""
    url = f"https://dns.google/resolve?name={hostname}&type=A"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            for answer in data.get("Answer", []):
                if answer.get("type") == 1:  # A record
                    return answer["data"]
    except Exception:
        pass
    return None


def patch_dns_if_needed():
    """If googleapis.com is unreachable via local DNS, resolve via DoH and
    monkey-patch socket.getaddrinfo to use the public IPs as a fallback."""
    if _check_reachable(GOOGLE_API_HOSTS[0]):
        return

    print("Google APIs unreachable via local DNS, resolving via DoH fallback...")
    overrides = {}
    for host in GOOGLE_API_HOSTS:
        ip = _resolve_via_doh(host)
        if ip:
            overrides[host] = ip
            print(f"  {host} -> {ip}")

    if not overrides:
        print("WARNING: DoH resolution failed, Google API calls may hang.")
        return

    _original_getaddrinfo = socket.getaddrinfo

    def _patched_getaddrinfo(host, port, *args, **kwargs):
        if host in overrides:
            host = overrides[host]
        return _original_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = _patched_getaddrinfo

    # httplib2 (used by google-api-python-client) does its own connection
    # handling and may bypass getaddrinfo. Patch its HTTPSConnectionWithTimeout
    # to connect to the resolved IP while preserving the original hostname for
    # SNI and certificate verification.
    try:
        import httplib2
        _original_connect = httplib2.HTTPSConnectionWithTimeout.connect

        def _patched_connect(self):
            if self.host in overrides:
                real_host = self.host
                self.host = overrides[real_host]
                # Create TCP connection to the IP
                sock = socket.create_connection(
                    (self.host, self.port),
                    timeout=self.timeout,
                )
                # Wrap with TLS using the original hostname for SNI
                self.sock = self._context.wrap_socket(
                    sock, server_hostname=real_host
                )
                # Restore the original hostname
                self.host = real_host
            else:
                _original_connect(self)

        httplib2.HTTPSConnectionWithTimeout.connect = _patched_connect
    except (ImportError, AttributeError):
        pass

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
DRIVE_FOLDER_NAME = "claude-code-history"
SCRIPT_DIR = Path(__file__).parent
STATE_DIR = Path(os.environ.get("SYNC_STATE_DIR", str(SCRIPT_DIR)))
TOKEN_PATH = SCRIPT_DIR / "token.json"
CREDENTIALS_PATH = SCRIPT_DIR / "credentials.json"
SERVICE_ACCOUNT_PATH = SCRIPT_DIR / "service-account.json"
CODEX_DRIVE_PREFIX = "_codex__"


# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------

def get_drive_service():
    """Authenticate with Google Drive.

    Tries service account first (headless-friendly), falls back to OAuth.
    """
    # Option 1: Service account (no browser needed, works on all machines)
    if SERVICE_ACCOUNT_PATH.exists():
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            str(SERVICE_ACCOUNT_PATH), scopes=SCOPES
        )
        return build("drive", "v3", credentials=creds)

    # Option 2: OAuth (needs browser on first run per machine)
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Token refresh failed: {e}")
                print("Removing stale token, re-authenticating...")
                TOKEN_PATH.unlink(missing_ok=True)
                creds = None
        if not creds or not creds.valid:
            if not CREDENTIALS_PATH.exists():
                print(f"ERROR: No auth credentials found.")
                print(f"Place one of these in {SCRIPT_DIR}:")
                print(f"  service-account.json  (recommended for headless)")
                print(f"  credentials.json      (OAuth, needs browser once)")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            try:
                creds = flow.run_local_server(port=0)
            except OSError:
                # Headless: no browser available, use console-based flow
                print("No browser available. Visit this URL on any device:")
                auth_url, _ = flow.authorization_url(prompt="consent")
                print(f"\n  {auth_url}\n")
                code = input("Enter the authorization code: ").strip()
                flow.fetch_token(code=code)
                creds = flow.credentials
        TOKEN_PATH.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds)


def get_or_create_folder(service, folder_name, parent_id=None):
    q = (
        f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'"
        f" and trashed=false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"
    results = service.files().list(q=q, fields="files(id,name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        meta["parents"] = [parent_id]
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def list_drive_folders(service, parent_id, include_description=False):
    """List subfolders. Returns {name: id} or {name: {id, description}} if include_description."""
    folders = {}
    fields = "nextPageToken, files(id, name)"
    if include_description:
        fields = "nextPageToken, files(id, name, description)"
    page_token = None
    while True:
        results = (
            service.files()
            .list(
                q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields=fields,
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        for f in results.get("files", []):
            if include_description:
                folders[f["name"]] = {"id": f["id"], "description": f.get("description", "")}
            else:
                folders[f["name"]] = f["id"]
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return folders


def list_remote_files(service, folder_id):
    """List all files in a Drive folder. Returns {name: {id, modifiedTime, md5, size}}."""
    remote = {}
    page_token = None
    while True:
        results = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false"
                f" and mimeType!='application/vnd.google-apps.folder'",
                fields="nextPageToken, files(id, name, modifiedTime, md5Checksum, size)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        for f in results.get("files", []):
            remote[f["name"]] = f
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return remote


def batch_list_drive_folders(service, folder_ids):
    """List subfolders in multiple Drive folders using batch requests.

    Args: folder_ids: dict of {key: folder_id}
    Returns: {key: {subfolder_name: subfolder_id}}
    """
    results = {k: {} for k in folder_ids}
    items = list(folder_ids.items())

    for batch_start in range(0, len(items), 100):
        batch_items = items[batch_start:batch_start + 100]
        batch = service.new_batch_http_request()

        def _make_callback(key):
            def _cb(request_id, response, exception):
                if exception is None and response:
                    for f in response.get("files", []):
                        results[key][f["name"]] = f["id"]
            return _cb

        for key, fid in batch_items:
            req = service.files().list(
                q=f"'{fid}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id, name)",
                pageSize=1000,
            )
            batch.add(req, callback=_make_callback(key))

        batch.execute()

    return results


def batch_list_remote_files(service, folder_ids):
    """List files in multiple Drive folders using batch requests.

    Args: folder_ids: dict of {key: folder_id}
    Returns: {key: {name: {id, modifiedTime, md5, size}}}
    """
    results = {k: {} for k in folder_ids}
    items = list(folder_ids.items())

    # Google batch API supports up to 100 requests per batch
    for batch_start in range(0, len(items), 100):
        batch_items = items[batch_start:batch_start + 100]
        batch = service.new_batch_http_request()

        def _make_callback(key):
            def _cb(request_id, response, exception):
                if exception is None and response:
                    for f in response.get("files", []):
                        results[key][f["name"]] = f
            return _cb

        for key, fid in batch_items:
            req = service.files().list(
                q=f"'{fid}' in parents and trashed=false"
                f" and mimeType!='application/vnd.google-apps.folder'",
                fields="files(id, name, modifiedTime, md5Checksum, size)",
                pageSize=1000,
            )
            batch.add(req, callback=_make_callback(key))

        batch.execute()

    return results


def upload_string(service, content: str, name: str, folder_id: str, existing_id=None):
    """Upload a string as a file to Drive."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        media = MediaFileUpload(tmp_path, mimetype="application/json")
        if existing_id:
            service.files().update(fileId=existing_id, media_body=media).execute()
        else:
            service.files().create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
            ).execute()
    finally:
        os.unlink(tmp_path)


def download_file(service, file_id: str, local_path: Path):
    """Download a file from Drive."""
    request = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def download_string(service, file_id: str) -> str:
    """Download a file from Drive as a string."""
    import io
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8")


# ---------------------------------------------------------------------------
# Git remote / local project resolution
# ---------------------------------------------------------------------------

def normalize_git_url(url: str) -> str:
    """Normalize git remote URL to a stable folder name.

    git@github.com:flashinfer-ai/flashinfer.git -> github.com__flashinfer-ai__flashinfer
    https://github.com/flashinfer-ai/flashinfer.git -> github.com__flashinfer-ai__flashinfer
    """
    url = url.strip()
    # SSH format: git@host:org/repo.git
    m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    if m:
        host, path = m.group(1), m.group(2)
        return f"{host}__{path.replace('/', '__')}"
    # HTTPS format: https://host/org/repo.git
    m = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?$", url)
    if m:
        host, path = m.group(1), m.group(2)
        return f"{host}__{path.replace('/', '__')}"
    # Fallback: sanitize
    return re.sub(r"[^\w.-]", "__", url)


def resolve_claude_project_path(project_dir_name: str) -> str | None:
    """Convert claude project dir name to local filesystem path.

    Claude encodes /sgl-workspace/cutlass/examples/python/CuTeDSL/blackwell/flash-attention
    as -sgl-workspace-cutlass-examples-python-CuTeDSL-blackwell-flash-attention

    The problem: real directory names can contain hyphens (e.g. flash-attention),
    and Claude also maps underscores to hyphens. We try all possible split points
    and both - and _ variants, checking which paths exist on disk.
    """
    encoded = project_dir_name.lstrip("-")
    segments = encoded.split("-")

    dir_cache = {}

    def _is_dir(p):
        if p not in dir_cache:
            dir_cache[p] = Path(p).is_dir()
        return dir_cache[p]

    def _resolve(pos: int, current_path: str, component_start: int) -> str | None:
        """Recursively try combining segments with /, -, _, or . at each split point.
        component_start tracks which segment the current component began at,
        to limit component length and prune dead branches."""
        if pos == len(segments):
            if Path(current_path).exists():
                return current_path
            return None

        # Option 1: treat current_path as a complete dir, start new component
        if _is_dir(current_path):
            candidate = current_path + "/" + segments[pos]
            result = _resolve(pos + 1, candidate, pos)
            if result:
                return result

        # Options 2-4: continue building current component name
        # Limit: a single component can span at most 4 segments to avoid combinatorial explosion
        if pos - component_start < 4:
            for sep in ("-", "_", "."):
                candidate = current_path + sep + segments[pos]
                result = _resolve(pos + 1, candidate, component_start)
                if result:
                    return result

        return None

    if not segments:
        return None

    # Start with /first-segment as the root
    return _resolve(1, "/" + segments[0], 0)


def find_git_root(path: str) -> str | None:
    """Walk up from path to find the nearest git root."""
    p = Path(path)
    while p != p.parent:
        if (p / ".git").exists():
            return str(p)
        p = p.parent
    return None


def get_git_remote(repo_path: str) -> str | None:
    """Get a remote URL for a local git repo. Tries origin first, then first available."""
    try:
        # Try origin first
        result = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()

        # No origin — list all remotes and pick the first
        result = subprocess.run(
            ["git", "-C", repo_path, "remote"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            remotes = result.stdout.strip().split("\n")
            for remote_name in remotes:
                remote_name = remote_name.strip()
                if not remote_name:
                    continue
                url_result = subprocess.run(
                    ["git", "-C", repo_path, "remote", "get-url", remote_name],
                    capture_output=True, text=True, timeout=5,
                )
                if url_result.returncode == 0:
                    return url_result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def rel_path_to_drive_subfolder(rel_path: str) -> str:
    """Convert a relative path within a repo to a Drive subfolder name.

    '.' (repo root) -> '_root'
    'flash_attn/cute' -> 'flash_attn__cute'
    """
    if rel_path == ".":
        return "_root"
    return rel_path.replace("/", "__")


def drive_subfolder_to_rel_path(subfolder: str) -> str:
    """Inverse of rel_path_to_drive_subfolder."""
    if subfolder == "_root":
        return "."
    return subfolder.replace("__", "/")


def codex_rel_path_to_drive_subfolder(rel_path: str) -> str:
    """Drive subfolder for Codex rollouts at a repo-relative cwd.

    Codex rollouts share the same repo/path folders as Claude conversations;
    individual rollout filenames carry the Codex prefix.
    """
    return rel_path_to_drive_subfolder(rel_path)


def is_codex_drive_subfolder(subfolder: str) -> bool:
    """Legacy Codex-only path folder marker."""
    return subfolder.startswith(CODEX_DRIVE_PREFIX)


def codex_drive_subfolder_to_rel_path(subfolder: str) -> str:
    if not is_codex_drive_subfolder(subfolder):
        return drive_subfolder_to_rel_path(subfolder)
    suffix = subfolder[len(CODEX_DRIVE_PREFIX):]
    if suffix == "root":
        return "."
    return suffix.replace("__", "/")


def codex_local_name_from_remote(fname: str) -> str:
    """Convert a Drive Codex filename to its local rollout filename."""
    if fname.startswith(CODEX_DRIVE_PREFIX):
        return fname[len(CODEX_DRIVE_PREFIX):]
    return fname


def codex_remote_name_from_local(fname: str) -> str:
    """Convert a local rollout filename to the prefixed Drive filename."""
    if fname.startswith(CODEX_DRIVE_PREFIX):
        return fname
    return CODEX_DRIVE_PREFIX + fname


def is_codex_remote_file(fname: str) -> bool:
    return fname.startswith(CODEX_DRIVE_PREFIX) and fname.endswith(".jsonl")


def is_jsonl_conversation_file(fname: str) -> bool:
    return fname.endswith(".jsonl") and not is_codex_remote_file(fname)


def codex_group_slug(title: str | None) -> str:
    """Create a short lowercase slug from a codex rollout title for grouping."""
    if not title:
        return "_untitled"
    t = title.strip()
    if t.startswith("#"):
        t = t.lstrip("# ").split("\n")[0]
    t = t.split("\n")[0][:80]
    slug = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return slug[:60] or "_untitled"


def codex_name_matches_chat_filters(fname: str, chat_filters: list[str] | None,
                                     title: str | None = None,
                                     session_id: str | None = None) -> bool:
    if not chat_filters:
        return True
    local_name = codex_local_name_from_remote(fname)
    stem = local_name[:-6] if local_name.endswith(".jsonl") else local_name
    slug = codex_group_slug(title) if title else ""
    title_lower = (title or "").lower()
    sid = session_id or ""
    return any(
        local_name.startswith(c)
        or stem.startswith(c)
        or stem.endswith(c)
        or f"-{c}" in stem
        or (slug and c.lower() in slug)
        or (title_lower and c.lower() in title_lower)
        or (sid and sid.startswith(c))
        for c in chat_filters
    )


def is_gitignored(git_root: str, rel_path: str) -> bool:
    """Check if a relative path (or any parent) is gitignored in the repo."""
    if not git_root or rel_path == ".":
        return False
    # Check the path and all parent components
    parts = Path(rel_path).parts
    for i in range(1, len(parts) + 1):
        check = str(Path(*parts[:i]))
        try:
            result = subprocess.run(
                ["git", "-C", git_root, "check-ignore", "-q", check],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    return False


def _parse_time_value(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Codex sqlite stores seconds in state_5 today, but handle ms too.
        return float(value / 1000 if value > 10_000_000_000 else value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _iter_jsonl_objects(jsonl_path: Path):
    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (OSError, UnicodeDecodeError):
        return


def parse_codex_rollout(jsonl_path: Path) -> dict:
    """Read stable metadata from a Codex rollout JSONL file."""
    meta = {
        "session_id": None,
        "cwd": None,
        "git_url": None,
        "created_at": None,
        "updated_at": None,
        "first_user_message": None,
    }
    for entry in _iter_jsonl_objects(jsonl_path):
        ts = _parse_time_value(entry.get("timestamp"))
        if ts is not None:
            meta["updated_at"] = max(meta["updated_at"] or ts, ts)
            if meta["created_at"] is None:
                meta["created_at"] = ts

        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        typ = entry.get("type")
        if typ == "session_meta":
            meta["session_id"] = meta["session_id"] or payload.get("id")
            meta["cwd"] = meta["cwd"] or payload.get("cwd")
            meta["created_at"] = meta["created_at"] or _parse_time_value(payload.get("timestamp"))
            git_info = payload.get("git")
            if isinstance(git_info, dict):
                meta["git_url"] = meta["git_url"] or git_info.get("repository_url")
        elif typ == "turn_context":
            meta["cwd"] = meta["cwd"] or payload.get("cwd")
        elif meta["first_user_message"] is None:
            item = payload.get("item") if isinstance(payload, dict) else None
            if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user":
                content = item.get("content")
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            parts.append(part["text"])
                    if parts:
                        meta["first_user_message"] = " ".join(parts).strip()
                elif isinstance(content, str):
                    meta["first_user_message"] = content.strip()

    if meta["updated_at"] is None:
        try:
            meta["updated_at"] = jsonl_path.stat().st_mtime
        except OSError:
            pass
    return meta


def _load_codex_session_index(codex_home: Path | None = None) -> dict:
    codex_home = Path(codex_home or CODEX_HOME).expanduser()
    index_path = codex_home / "session_index.jsonl"
    by_id = {}
    if not index_path.exists():
        return by_id
    for entry in _iter_jsonl_objects(index_path):
        sid = entry.get("id")
        if sid:
            by_id[sid] = entry
    return by_id


def _load_codex_sqlite_threads(codex_home: Path | None = None) -> dict:
    codex_home = Path(codex_home or CODEX_HOME).expanduser()
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return {}
    rows = {}
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        available = {
            row["name"]
            for row in con.execute("pragma table_info(threads)")
        }
        required = {"id", "rollout_path"}
        if not required.issubset(available):
            return {}
        wanted = [
            "id",
            "rollout_path",
            "cwd",
            "title",
            "git_origin_url",
            "updated_at",
            "created_at",
            "first_user_message",
            "preview",
        ]
        columns = [c for c in wanted if c in available]
        cur = con.execute(
            f"select {', '.join(columns)} from threads where rollout_path is not null"
        )
        for row in cur:
            rollout_path = Path(str(row["rollout_path"])).expanduser()
            data = dict(row)
            if not data.get("first_user_message") and data.get("preview"):
                data["first_user_message"] = data["preview"]
            rows[str(rollout_path)] = data
    except sqlite3.Error:
        return {}
    finally:
        if con is not None:
            con.close()
    return rows


def _codex_rollout_path_for_remote_name(fname: str, codex_home: Path | None = None) -> Path:
    codex_home = Path(codex_home or CODEX_HOME).expanduser()
    m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", fname)
    if m:
        yyyy, mm, dd = m.groups()
        return codex_home / "sessions" / yyyy / mm / dd / fname
    return codex_home / "sessions" / "synced" / fname


def _codex_local_cwd(git_root: str | None, rel_path: str | None) -> str | None:
    if not git_root:
        return None
    if not rel_path or rel_path == ".":
        return git_root
    return os.path.join(git_root, rel_path)


def _first_codex_cwd_in_file(jsonl_path: Path) -> str | None:
    for entry in _iter_jsonl_objects(jsonl_path):
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if entry.get("type") in ("session_meta", "turn_context") and "cwd" in payload:
            return payload["cwd"]
    return None


def rewrite_codex_cwd_if_needed(jsonl_path: Path, local_cwd: str) -> int:
    """Rewrite Codex rollout cwd fields to the local repo path, preserving mtime."""
    if not local_cwd:
        return 0
    first = _first_codex_cwd_in_file(jsonl_path)
    if first is None or first == local_cwd:
        return 0
    try:
        lines = jsonl_path.read_text().splitlines(keepends=True)
        before_stat = jsonl_path.stat()
    except (OSError, UnicodeDecodeError):
        return 0

    rewrote = 0
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
        if entry.get("type") in ("session_meta", "turn_context") and payload and payload.get("cwd") != local_cwd:
            payload["cwd"] = local_cwd
            rewrote += 1
            out.append(json.dumps(entry, ensure_ascii=False) + "\n")
        else:
            out.append(line)

    if rewrote:
        try:
            jsonl_path.write_text("".join(out))
            os.utime(jsonl_path, (before_stat.st_atime, before_stat.st_mtime))
        except OSError:
            return 0
    return rewrote


def build_codex_index(codex_home: Path | None = None) -> dict:
    """Scan Codex rollout files and group them by normalized git remote URL."""
    codex_home = Path(codex_home or CODEX_HOME).expanduser()
    sqlite_rows = _load_codex_sqlite_threads(codex_home)
    session_titles = _load_codex_session_index(codex_home)

    rollout_paths = set()
    sessions_dir = codex_home / "sessions"
    if sessions_dir.exists():
        rollout_paths.update(sessions_dir.glob("**/rollout-*.jsonl"))
    for p in sqlite_rows:
        pp = Path(p).expanduser()
        if pp.exists():
            rollout_paths.add(pp)

    local_repos = None
    index = {}
    for rollout_path in sorted(rollout_paths):
        parsed = parse_codex_rollout(rollout_path)
        row = sqlite_rows.get(str(rollout_path), {})
        session_id = parsed.get("session_id") or row.get("id") or rollout_path.stem
        cwd = row.get("cwd") or parsed.get("cwd")
        git_url = row.get("git_origin_url") or parsed.get("git_url")
        git_root = find_git_root(cwd) if cwd and Path(cwd).exists() else None
        if git_root:
            git_url = get_git_remote(git_root) or git_url
            rel_path = os.path.relpath(cwd, git_root)
        else:
            rel_path = "."
            if git_url:
                local_repos = local_repos if local_repos is not None else scan_local_git_repos()
                match = local_repos.get(normalize_git_url(git_url))
                if match:
                    git_root, git_url = match
        if git_root and rel_path and is_gitignored(git_root, rel_path):
            continue

        title_info = session_titles.get(session_id, {})
        thread_name = title_info.get("thread_name")
        sqlite_title = row.get("title")
        if sqlite_title and len(sqlite_title) > 120:
            sqlite_title = None
        title = (
            thread_name
            or sqlite_title
            or parsed.get("first_user_message")
            or row.get("first_user_message")
        )
        entry = {
            "path": rollout_path,
            "session_id": session_id,
            "title": title,
            "cwd": cwd,
            "git_url": git_url,
            "git_root": git_root,
            "rel_path": rel_path,
            "updated_at": _parse_time_value(row.get("updated_at")) or parsed.get("updated_at"),
        }
        key = normalize_git_url(git_url) if git_url else None
        index.setdefault(key, []).append(entry)
    return index


def upsert_codex_session_index(entries: list[dict], codex_home: Path | None = None):
    """Make pulled Codex rollouts discoverable by session_index.jsonl."""
    if not entries:
        return
    codex_home = Path(codex_home or CODEX_HOME).expanduser()
    index_path = codex_home / "session_index.jsonl"
    existing = []
    by_id = {}
    if index_path.exists():
        for entry in _iter_jsonl_objects(index_path):
            sid = entry.get("id")
            if sid:
                by_id[sid] = entry
            existing.append(entry)
    for entry in entries:
        sid = entry.get("session_id")
        if not sid:
            continue
        updated = entry.get("updated_at")
        updated_dt = (
            datetime.fromtimestamp(updated, tz=timezone.utc)
            if updated
            else datetime.now(timezone.utc)
        )
        updated_iso = updated_dt.replace(tzinfo=None).isoformat() + "Z"
        new_name = entry.get("title") or entry.get("first_user_message")
        existing_name = by_id.get(sid, {}).get("thread_name")
        if existing_name and existing_name != sid and not new_name:
            new_name = existing_name
        by_id[sid] = {
            "id": sid,
            "thread_name": new_name or sid,
            "updated_at": updated_iso,
        }
    seen = set()
    merged = []
    for entry in existing:
        sid = entry.get("id")
        if sid in by_id and sid not in seen:
            merged.append(by_id[sid])
            seen.add(sid)
        elif sid not in seen:
            merged.append(entry)
            if sid:
                seen.add(sid)
    for sid, entry in sorted(by_id.items()):
        if sid not in seen:
            merged.append(entry)

    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(index_path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in merged))
    tmp.replace(index_path)


REPO_CACHE_PATH = SCRIPT_DIR / ".repo_cache.json"


def scan_local_git_repos() -> dict:
    """Scan the parent directory of this script's repo for all git repos.

    Returns: {normalized_git_url: (git_root, raw_url)}
    Only scans up to 3 levels deep to avoid traversing huge source trees.
    """
    scan_root = SCRIPT_DIR.parent
    repos = {}
    max_depth = 3
    root_depth = str(scan_root).count(os.sep)
    for dirpath, dirnames, _ in os.walk(scan_root):
        if ".git" in dirnames:
            git_root = dirpath
            git_url = get_git_remote(git_root)
            if git_url:
                key = normalize_git_url(git_url)
                repos[key] = (git_root, git_url)
            dirnames.clear()
            continue
        current_depth = dirpath.count(os.sep) - root_depth
        if current_depth >= max_depth:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
    return repos


def load_repo_cache() -> dict:
    """Load cached project_dir_name -> {git_root, git_url, rel_path} mapping."""
    if REPO_CACHE_PATH.exists():
        try:
            return json.loads(REPO_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_repo_cache(cache: dict):
    """Save project_dir_name -> resolved info cache."""
    REPO_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def build_local_index() -> dict:
    """Scan all local claude project dirs.

    Returns: {normalized_git_url: [(project_dir, git_url, git_root, rel_path), ...]}
    - project_dir: Path to ~/.claude/projects/<name>
    - git_url: raw git remote URL
    - git_root: absolute path to the git root
    - rel_path: path from git_root to the project dir (e.g. '.' or 'flash_attn/cute')
    Projects without a git remote are grouped under key None.
    """
    repo_cache = load_repo_cache()
    cache_changed = False

    index = {}
    if not CLAUDE_PROJECTS_DIR.exists():
        return index
    for d in CLAUDE_PROJECTS_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue

        fs_path = resolve_claude_project_path(d.name)
        git_root = find_git_root(fs_path) if fs_path else None
        git_url = get_git_remote(git_root) if git_root else None

        # Fallback: check cache from a previous run
        if not git_url and d.name in repo_cache:
            cached = repo_cache[d.name]
            cached_root = cached.get("git_root")
            if cached_root and Path(cached_root).is_dir():
                git_root = cached_root
                git_url = get_git_remote(git_root)
                rel_path_cached = cached.get("rel_path", ".")
                # Skip gitignored paths even when the directory no longer exists
                if is_gitignored(git_root, rel_path_cached):
                    continue
                fs_path = cached_root
                if rel_path_cached != ".":
                    candidate = os.path.join(git_root, rel_path_cached)
                    if Path(candidate).is_dir():
                        fs_path = candidate

        if git_url and git_root and fs_path:
            key = normalize_git_url(git_url)
            rel_path = os.path.relpath(fs_path, git_root)
            # Skip gitignored paths
            if is_gitignored(git_root, rel_path):
                continue
            if d.name not in repo_cache or repo_cache[d.name].get("git_root") != git_root:
                repo_cache[d.name] = {
                    "git_root": git_root,
                    "git_url": git_url,
                    "rel_path": rel_path,
                }
                cache_changed = True
        else:
            key = None
            rel_path = None

        index.setdefault(key, []).append((d, git_url, git_root, rel_path))

    if cache_changed:
        save_repo_cache(repo_cache)
    return index


def resolve_unmatched_projects(index):
    """Resolve unresolved projects by scanning local sibling repos for matching git remotes.

    For projects from other machines where the encoded path doesn't exist locally,
    scan repos in the parent directory of this script to find one whose git remote
    matches. The project dir name ends with the repo name, so we match on that.
    Once matched, we try to reconstruct the relative path within the repo.
    """
    unresolved = index.pop(None, [])
    if not unresolved:
        return

    local_repos = scan_local_git_repos()
    if not local_repos:
        index.setdefault(None, []).extend(unresolved)
        return

    # Build reverse index: repo basename -> [(normalized_url, git_root, raw_url)]
    # to match project dir names that end with the repo name
    repos_by_name = {}
    for norm_url, (git_root, raw_url) in local_repos.items():
        basename = Path(git_root).name.lower()
        repos_by_name.setdefault(basename, []).append((norm_url, git_root, raw_url))

    repo_cache = load_repo_cache()
    cache_changed = False
    still_unresolved = []

    for project_dir, _, _, _ in unresolved:
        segments = project_dir.name.lstrip("-").split("-")
        matched = False

        # Try matching the tail of the project dir name against repo basenames
        # e.g. -mlx-devbox-users-foo-playground-sglang -> try "sglang"
        # e.g. -mlx-devbox-...-flash-attention-fp4 -> try "fp4", "attention-fp4", "flash-attention-fp4"
        for i in range(len(segments) - 1, max(0, len(segments) - 5) - 1, -1):
            candidate_name = "-".join(segments[i:]).lower()
            if candidate_name in repos_by_name:
                norm_url, git_root, raw_url = repos_by_name[candidate_name][0]

                # Reconstruct relative path: everything between repo name and
                # the end of the project dir path. The segments before the repo
                # name are the machine path, segments after (if any) are subdirs.
                # For now, assume repo root unless we can resolve further.
                rel_path = "."

                # Try to resolve subdir within the repo from remaining segments
                # The repo name matched at position i, so segments after a possible
                # repo-name match could be subdirs
                # e.g. -...-flash-attention-fp4-flash-attn-cute
                #   repo = flash-attention-fp4 (matched at i)
                #   remaining after repo = flash-attn-cute -> flash_attn/cute
                repo_segments = candidate_name.split("-")
                repo_end_idx = i + len(repo_segments)
                if repo_end_idx < len(segments):
                    remaining = segments[repo_end_idx:]
                    # Try to resolve remaining as a subpath within the repo
                    from sync_claude_history import resolve_claude_project_path
                    # Build candidate subpaths
                    sub_encoded = "-".join(remaining)
                    # Try each combo of - / _ / / for the remaining segments
                    def _resolve_sub(pos, current):
                        if pos == len(remaining):
                            full = os.path.join(git_root, current) if current else git_root
                            if Path(full).is_dir():
                                return current or "."
                            return None
                        seg = remaining[pos]
                        for sep in ["/", "-", "_"]:
                            cand = (current + sep + seg) if current else seg
                            result = _resolve_sub(pos + 1, cand)
                            if result:
                                return result
                        return None

                    resolved_sub = _resolve_sub(0, "")
                    if resolved_sub:
                        rel_path = resolved_sub

                key = norm_url
                index.setdefault(key, []).append(
                    (project_dir, raw_url, git_root, rel_path)
                )
                repo_cache[project_dir.name] = {
                    "git_root": git_root,
                    "git_url": raw_url,
                    "rel_path": rel_path,
                }
                cache_changed = True
                matched = True
                break

        if not matched:
            still_unresolved.append((project_dir, None, None, None))

    if still_unresolved:
        index.setdefault(None, []).extend(still_unresolved)

    if cache_changed:
        save_repo_cache(repo_cache)


# ---------------------------------------------------------------------------
# File-level sync logic
# ---------------------------------------------------------------------------

def is_empty_conversation(jsonl_path: Path) -> bool:
    """Check if a conversation is empty (no assistant response).

    Empty conversations are created when you open Claude and immediately exit,
    or only type /resume. They contain only file-history-snapshot, user meta,
    and local-command entries, but no assistant messages.
    """
    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "assistant":
                    return False
    except (OSError, UnicodeDecodeError):
        pass
    return True


def _file_has_compact_marker(jsonl_path: Path) -> bool:
    """Fast check for the ``isCompactSummary`` marker in a file (bytes-level).

    Avoids parsing the entire JSONL on every sync when the file has no
    compact point (the common case for fresh conversations).
    """
    needle = b'"isCompactSummary"'
    try:
        with open(jsonl_path, "rb") as f:
            # Read in chunks so we don't slurp huge files
            overlap = b""
            while True:
                chunk = f.read(1 << 20)  # 1 MiB
                if not chunk:
                    return False
                if needle in overlap + chunk:
                    return True
                # Keep last len(needle)-1 bytes as overlap for boundary matches
                overlap = chunk[-(len(needle) - 1):]
    except OSError:
        return False


def trim_at_last_compact(jsonl_path: Path) -> tuple[bool, int, int]:
    """Trim a conversation at the last ``isCompactSummary`` entry.

    Keeps any leading ``type=summary`` entries (title metadata), the last
    ``isCompactSummary`` entry itself (with its ``parentUuid`` patched to
    ``None`` so it acts as the new root), and every entry that follows it.
    Everything before the last compact point is discarded — that content is
    already captured by the summary message itself, and dropping it lets
    ``claude --resume`` load huge conversations that would otherwise exceed
    the context window at load time.

    A ``.pretrim.bak`` copy is written next to the file (only if one does not
    already exist) before rewriting.

    Returns ``(trimmed, before_bytes, after_bytes)``. When no compact point is
    found, or the compact point is already at the top of the file, the file
    is left untouched and ``trimmed`` is ``False``.
    """
    import shutil

    # Fast path: no compact marker anywhere in the file
    if not _file_has_compact_marker(jsonl_path):
        try:
            size = jsonl_path.stat().st_size
        except OSError:
            size = 0
        return False, size, size

    try:
        with open(jsonl_path, "r") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        size = jsonl_path.stat().st_size
        return False, size, size

    parsed: list[dict | None] = [None] * len(lines)
    compact_idx = -1
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed[i] = d
        if d.get("isCompactSummary") is True:
            compact_idx = i

    before_stat = jsonl_path.stat()
    before_size = before_stat.st_size
    before_mtime = before_stat.st_mtime
    if compact_idx < 0:
        return False, before_size, before_size

    # Preserve leading contiguous block of ``type=summary`` entries
    leading_summary_end = 0
    for i, d in enumerate(parsed):
        if d is None:
            continue
        if d.get("type") == "summary":
            leading_summary_end = i + 1
        else:
            break

    if compact_idx < leading_summary_end:
        return False, before_size, before_size

    # Idempotency check: if the compact entry is already the first
    # non-summary line, the file is already trimmed and nothing needs to
    # change.  Skip the rewrite to avoid churning mtime/md5 every sync cycle.
    if compact_idx == leading_summary_end:
        return False, before_size, before_size

    compact_entry = parsed[compact_idx]
    compact_entry = dict(compact_entry)
    # Keep the original parentUuid — a dangling reference to a removed entry
    # is fine.  Nulling it out breaks Claude Code's tree-walking on resume:
    # CC treats parentUuid=None as a brand-new session root and creates a
    # disconnected branch instead of loading the compaction summary.

    bak = jsonl_path.with_suffix(jsonl_path.suffix + ".pretrim.bak")
    if not bak.exists():
        shutil.copy2(jsonl_path, bak)

    # Collect UUIDs that will survive the trim (compact entry + everything
    # after it, plus leading summaries).
    surviving_uuids = set()
    for i in range(leading_summary_end):
        if parsed[i] is not None:
            u = parsed[i].get("uuid")
            if u:
                surviving_uuids.add(u)
    surviving_uuids.add(compact_entry.get("uuid", ""))
    for i in range(compact_idx + 1, len(parsed)):
        if parsed[i] is not None:
            u = parsed[i].get("uuid")
            if u:
                surviving_uuids.add(u)

    # Fix dangling parentUuids: entries after the compact point whose parent
    # was in the trimmed portion.  Reconnect them to the compact entry so the
    # conversation tree stays fully connected for Claude Code's resume.
    compact_uuid = compact_entry.get("uuid")
    fixed_lines: dict[int, str] = {}   # line_idx -> rewritten JSON line
    for i in range(compact_idx + 1, len(parsed)):
        d = parsed[i]
        if d is None:
            continue
        parent = d.get("parentUuid")
        if parent and parent not in surviving_uuids:
            d = dict(d)
            d["parentUuid"] = compact_uuid
            fixed_lines[i] = json.dumps(d, ensure_ascii=False) + "\n"

    tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".trim.tmp")
    with open(tmp, "w") as f:
        for i in range(leading_summary_end):
            if parsed[i] is None:
                continue
            line = lines[i]
            f.write(line if line.endswith("\n") else line + "\n")
        f.write(json.dumps(compact_entry, ensure_ascii=False) + "\n")
        for i in range(compact_idx + 1, len(lines)):
            if not lines[i].strip():
                continue
            if i in fixed_lines:
                f.write(fixed_lines[i])
            else:
                line = lines[i]
                f.write(line if line.endswith("\n") else line + "\n")

    tmp.replace(jsonl_path)
    # Preserve original mtime so that auto-trim during sync does not flip the
    # push/pull direction for conversations that haven't actually been updated.
    try:
        os.utime(jsonl_path, (before_stat.st_atime, before_mtime))
    except OSError:
        pass
    after_size = jsonl_path.stat().st_size
    return True, before_size, after_size


def repair_uuid_chain(jsonl_path: Path) -> int:
    """Fix broken parentUuid links and remove orphan/duplicate entries.

    Claude Code sometimes writes entries whose parentUuid points to a uuid that
    was never persisted (e.g. after an ENOSPC crash or silent compaction), and
    also writes duplicate uuid entries on retry/sidechain branches.  Duplicate
    uuids cause CC's uuid→entry resolution to land on the wrong copy, making
    ``claude --resume`` unable to walk the full conversation chain.

    This function:
    1. Repairs broken parentUuid links by bridging to the nearest preceding entry
    2. Strips duplicate/orphan entries not on the main chain (reachable from tail)

    Returns the number of fixes applied (0 means the file was already clean).
    """
    try:
        with open(jsonl_path, "r") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return 0

    parsed: list[dict | None] = [None] * len(lines)
    uuid_lines: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed[i] = d
        uid = d.get("uuid")
        if uid:
            uuid_lines.setdefault(uid, []).append(i)

    if not uuid_lines:
        return 0

    # Use last occurrence for resolution (CC likely does the same)
    uuid_to_line = {uid: indices[-1] for uid, indices in uuid_lines.items()}

    # Phase 1: repair broken chain links
    fixes = 0
    while True:
        if fixes > 0:
            uuid_to_line = {uid: indices[-1] for uid, indices in uuid_lines.items()}

        tail = len(lines) - 1
        while tail >= 0 and (parsed[tail] is None or not parsed[tail].get("uuid")):
            tail -= 1
        if tail < 0:
            break

        broken_line = None
        current = tail
        visited: set[int] = set()
        while current is not None and current not in visited:
            visited.add(current)
            d = parsed[current]
            parent = d.get("parentUuid") if d else None
            if parent and parent in uuid_to_line:
                current = uuid_to_line[parent]
            elif parent and parent not in uuid_to_line and current > 0:
                broken_line = current
                break
            else:
                current = None

        if broken_line is None:
            break

        bridge = broken_line - 1
        while bridge >= 0 and (parsed[bridge] is None or not parsed[bridge].get("uuid")):
            bridge -= 1
        if bridge < 0:
            break

        d = dict(parsed[broken_line])
        d["parentUuid"] = parsed[bridge]["uuid"]
        parsed[broken_line] = d
        lines[broken_line] = json.dumps(d, ensure_ascii=False) + "\n"
        fixes += 1

    # Phase 2: check for duplicate uuids
    has_dupes = any(len(indices) > 1 for indices in uuid_lines.values())
    if not has_dupes and fixes == 0:
        return 0

    if has_dupes:
        # Walk main chain from tail
        uuid_to_line = {uid: indices[-1] for uid, indices in uuid_lines.items()}
        tail = len(lines) - 1
        while tail >= 0 and (parsed[tail] is None or not parsed[tail].get("uuid")):
            tail -= 1

        main_chain: set[int] = set()
        current = tail
        visited_dedup: set[int] = set()
        while current is not None and current not in visited_dedup:
            visited_dedup.add(current)
            main_chain.add(current)
            obj = parsed[current]
            parent = obj.get("parentUuid") if obj else None
            if parent and parent in uuid_to_line:
                current = uuid_to_line[parent]
            else:
                current = None

        chain_root = min(main_chain) if main_chain else 0
        keep: set[int] = set()
        for i in range(len(lines)):
            if i in main_chain:
                keep.add(i)
            elif parsed[i] is not None and not parsed[i].get("uuid"):
                # Keep metadata entries (custom-title, file-history-snapshot, etc.)
                # that are interspersed with the main chain
                if i >= chain_root:
                    keep.add(i)

        removed = len(lines) - len(keep)
        fixes += removed
    else:
        keep = None

    if fixes == 0:
        return 0

    before_stat = jsonl_path.stat()
    tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".repair.tmp")
    with open(tmp, "w") as f:
        for i in (sorted(keep) if keep is not None else range(len(lines))):
            line = lines[i]
            f.write(line if line.endswith("\n") else line + "\n")
    tmp.replace(jsonl_path)
    try:
        os.utime(jsonl_path, (before_stat.st_atime, before_stat.st_mtime))
    except OSError:
        pass
    return fixes


def merge_jsonl_by_timestamp(local_path: Path, remote_text: str) -> bool:
    """Merge local and remote JSONL when both sides have unique entries.

    When two machines background-sync the same conversation, each may append
    entries the other doesn't have.  Instead of one side winning (and losing
    the other's work), this merges both sets of entries:

    1. Union all entries by uuid (common entries kept once, unique from each side)
    2. Sort uuid'd entries by timestamp
    3. Rebuild the parentUuid chain in timestamp order
    4. Append non-uuid metadata entries from both sides (deduplicated)

    Returns True if a merge was performed, False if no conflict (one side is a
    strict superset of the other — normal push/pull handles that).
    """
    try:
        with open(local_path, "r") as f:
            local_lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return False

    remote_lines = remote_text.splitlines(keepends=True)
    if not remote_lines:
        return False

    def parse_entries(lines):
        uuid_entries = {}  # uuid -> (parsed_dict, raw_line)
        meta_lines = []    # lines without uuid
        for line in lines:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            uid = d.get("uuid")
            if uid:
                uuid_entries[uid] = (d, line if line.endswith("\n") else line + "\n")
            else:
                meta_lines.append((d, line if line.endswith("\n") else line + "\n"))
        return uuid_entries, meta_lines

    local_uuids, local_meta = parse_entries(local_lines)
    remote_uuids, remote_meta = parse_entries(remote_lines)

    local_only = local_uuids.keys() - remote_uuids.keys()
    remote_only = remote_uuids.keys() - local_uuids.keys()

    if not local_only or not remote_only:
        return False

    # Both sides have unique entries — merge
    merged = {}
    for uid in local_uuids.keys() | remote_uuids.keys():
        if uid in local_uuids:
            merged[uid] = local_uuids[uid]
        else:
            merged[uid] = remote_uuids[uid]

    # Sort by timestamp
    def sort_key(item):
        uid, (d, _) = item
        ts = d.get("timestamp", "")
        return ts if ts else ""

    sorted_entries = sorted(merged.items(), key=sort_key)

    # Rebuild parentUuid chain in timestamp order — every entry after the
    # first gets its parentUuid rewritten to point to the previous entry so
    # the merged conversation forms a single linear chain that CC can walk.
    prev_uuid = None
    rewritten = []
    for uid, (d, raw_line) in sorted_entries:
        if prev_uuid is not None:
            d = dict(d)
            d["parentUuid"] = prev_uuid
            raw_line = json.dumps(d, ensure_ascii=False) + "\n"
        prev_uuid = uid
        rewritten.append(raw_line)

    # Deduplicate metadata by serialized content
    seen_meta = set()
    merged_meta = []
    for d, raw_line in local_meta + remote_meta:
        key = raw_line.strip()
        if key not in seen_meta:
            seen_meta.add(key)
            merged_meta.append(raw_line)

    before_stat = local_path.stat()
    tmp = local_path.with_suffix(local_path.suffix + ".merge.tmp")
    with open(tmp, "w") as f:
        for line in rewritten:
            f.write(line)
        for line in merged_meta:
            f.write(line)
    tmp.replace(local_path)
    try:
        os.utime(local_path, (before_stat.st_atime, before_stat.st_mtime))
    except OSError:
        pass
    return True


def _first_cwd_in_file(jsonl_path: Path) -> str | None:
    """Return the first ``cwd`` value found in the file, or ``None``."""
    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "cwd" in d:
                    return d["cwd"]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def rewrite_cwd_if_needed(jsonl_path: Path, local_cwd: str) -> int:
    """Rewrite every entry's ``cwd`` field to ``local_cwd`` when it differs.

    Conversations synced across machines carry the original machine's absolute
    cwd (e.g. ``/sgl-workspace/claude-history-sync``) in every entry. Claude
    Code's ``--resume`` refuses to load a conversation whose cwd doesn't match
    the current working directory, so on any machine where the same repo lives
    at a different path the resume would fail with
    *"This conversation is from a different directory"*. Rewriting the cwd to
    the local project path on pull fixes that.

    Original mtime is preserved so this rewrite does not flip the push/pull
    direction or trigger spurious pushes on the next sync cycle.

    Returns the number of entries rewritten (0 if nothing changed).
    """
    if not local_cwd:
        return 0

    # Fast path: if the first entry with a cwd already matches ``local_cwd``,
    # assume the whole file is aligned (Claude Code writes a consistent cwd per
    # session). Skips the full JSON parse on every sync.
    first = _first_cwd_in_file(jsonl_path)
    if first is None or first == local_cwd:
        return 0

    try:
        with open(jsonl_path, "r") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return 0

    rewrote = 0
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            new_lines.append(line)
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue
        if "cwd" in d and d["cwd"] != local_cwd:
            d["cwd"] = local_cwd
            rewrote += 1
            new_lines.append(json.dumps(d, ensure_ascii=False) + "\n")
        else:
            new_lines.append(line)

    if rewrote == 0:
        return 0

    try:
        before_stat = jsonl_path.stat()
        with open(jsonl_path, "w") as f:
            f.writelines(new_lines)
        os.utime(jsonl_path, (before_stat.st_atime, before_stat.st_mtime))
    except OSError:
        return 0
    return rewrote


def local_file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def format_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f}KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f}MB"


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def get_conversation_title(jsonl_path: Path) -> str | None:
    """Extract the conversation title from a JSONL file.

    Looks for custom-title (user rename) first, falls back to slug (auto-generated).
    """
    custom_title = None
    slug = None
    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "custom-title":
                    custom_title = entry.get("customTitle")
                if slug is None and "slug" in entry:
                    slug = entry["slug"]
    except (OSError, UnicodeDecodeError):
        pass
    return custom_title or slug


def inject_custom_title(jsonl_path: Path, session_id: str, title: str):
    """Set the custom-title in a JSONL file. Replaces existing custom-title if present."""
    lines = []
    found = False
    new_entry = json.dumps({
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    })
    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    lines.append(line)
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    lines.append(line)
                    continue
                if entry.get("type") == "custom-title":
                    if entry.get("customTitle") == title:
                        return  # already correct, don't rewrite
                    # Replace with new title
                    lines.append(new_entry + "\n")
                    found = True
                else:
                    lines.append(line)
    except (OSError, UnicodeDecodeError):
        return

    if not found:
        lines.append(new_entry + "\n")

    with open(jsonl_path, "w") as f:
        f.writelines(lines)


def resolve_chat_id(prefix: str) -> tuple[str, str | None]:
    """Resolve a chat ID prefix to (full_session_id, repo_url).

    If multiple files match the prefix (e.g. the same session ID was synced
    into more than one project dir by a past sync bug), dedupe by session ID
    and pick the (project_dir, raw_url) of the largest file — that's the one
    Claude Code would actually use from its canonical location.

    Returns (prefix, None) if the session ID cannot be uniquely resolved.
    """
    matches = []  # (session_id, project_dir, size)
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob(f"{prefix}*.jsonl"):
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            matches.append((f.stem, project_dir, size))
    if not matches:
        codex_threads = _load_codex_sqlite_threads()
        codex_sessions = _load_codex_session_index()
        for rpath, row in codex_threads.items():
            sid = row.get("id", "")
            if sid.startswith(prefix):
                git_url = row.get("git_origin_url")
                if not git_url:
                    cwd = row.get("cwd")
                    git_root = find_git_root(cwd) if cwd and Path(cwd).exists() else None
                    if git_root:
                        git_url = get_git_remote(git_root)
                return sid, git_url
        for sid, info in codex_sessions.items():
            if sid.startswith(prefix):
                return sid, None
        return prefix, None

    # If multiple matches but they all share the same session ID, pick the
    # project dir whose file is largest (usually the canonical copy).
    unique_ids = {m[0] for m in matches}
    if len(unique_ids) != 1:
        return prefix, None

    session_id = next(iter(unique_ids))
    # Prefer a match whose project dir resolves to a git-rooted path; among
    # those, pick the largest file.
    candidates = sorted(matches, key=lambda m: -m[2])
    for _, project_dir, _ in candidates:
        local_path = resolve_claude_project_path(project_dir.name)
        if not local_path:
            continue
        git_root = find_git_root(local_path)
        if not git_root:
            continue
        raw_url = get_git_remote(git_root)
        if raw_url:
            return session_id, raw_url
    return session_id, None


def get_chat_name_by_id(chat_id: str | None) -> str | None:
    """Find the largest jsonl matching chat_id across project dirs and return its title."""
    if not chat_id:
        return None
    best = None  # (size, path)
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob(f"{chat_id}*.jsonl"):
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if best is None or size > best[0]:
                best = (size, f)
    if best is not None:
        return get_conversation_title(best[1])
    codex_sessions = _load_codex_session_index()
    for sid, info in codex_sessions.items():
        if sid.startswith(chat_id):
            return info.get("thread_name")
    codex_threads = _load_codex_sqlite_threads()
    for rpath, row in codex_threads.items():
        sid = row.get("id", "")
        if sid.startswith(chat_id):
            title = row.get("title", "")
            if len(title) <= 120:
                return title
            return row.get("first_user_message", "")[:80] or None
    return None


def resolve_repo_filter(repo_filter: str) -> str:
    """Resolve a repo substring filter to the full git remote URL. Returns filter if no unique match."""
    index = build_local_index()
    matches = set()
    for url_key, entries in index.items():
        for _, raw_url, _, _ in entries:
            if raw_url and repo_filter.lower() in raw_url.lower():
                matches.add(raw_url)
    if len(matches) == 1:
        return matches.pop()
    return repo_filter


def repo_matches_filter(raw_url: str, repo_filter: str | None) -> bool:
    """Check if a git remote URL matches the --repo filter (comma-separated substring match)."""
    if repo_filter is None:
        return True
    url_lower = raw_url.lower()
    return any(f.strip().lower() in url_lower for f in repo_filter.split(","))


def sync_files(service, folder_id, local_dir: Path, args, indent="    ",
               remote_files=None, local_jsons=None):
    """Sync .jsonl files between local_dir and a Drive folder. Returns (pushed, pulled, skipped)."""
    if remote_files is None:
        remote_files = list_remote_files(service, folder_id)
    remote_files = {
        name: info for name, info in remote_files.items()
        if not is_codex_remote_file(name)
    }
    if local_jsons is None:
        local_jsons = {p.name: p for p in sorted(local_dir.glob("*.jsonl"))
                       if not is_codex_remote_file(p.name) and not is_empty_conversation(p)}
    else:
        local_jsons = {
            name: path for name, path in local_jsons.items()
            if not is_codex_remote_file(name)
        }

    # Filter by --chat if specified (dest=chat_id)
    chat_ids = getattr(args, "chat_id", None)
    chat_filters = [c.strip() for c in chat_ids.split(",")] if chat_ids else None
    if chat_filters:
        local_jsons = {k: v for k, v in local_jsons.items()
                       if any(k.startswith(c) for c in chat_filters)}

    # Resolve this local project dir back to its absolute cwd so we can
    # rewrite cwd fields on pull (the pulled JSONL may carry a different
    # machine's absolute path, which would make ``claude --resume`` refuse).
    local_cwd = resolve_claude_project_path(local_dir.name)

    # Rewrite cwd fields on any existing local file whose entries still carry
    # another machine's cwd. This runs every sync so it self-heals files that
    # landed here from an older sync version. mtime is preserved inside
    # rewrite_cwd_if_needed so this doesn't flip the sync direction.
    if not args.dry_run and local_cwd:
        for fname, local_path in list(local_jsons.items()):
            try:
                n = rewrite_cwd_if_needed(local_path, local_cwd)
            except (OSError, ValueError):
                continue
            if n:
                print(f"{indent}[CWD REWRITE] {fname} "
                      f"({n} entries → {local_cwd})")

    # Auto-trim any local conversations at their last /compact point before
    # comparing against the remote. Claude Code's resume loader walks the whole
    # JSONL, so conversations that have been compacted keep piling up unusable
    # pre-compaction history — drop it now so the pushed version is also small
    # everywhere else the file gets synced to. Original mtime is preserved so
    # the sync direction is not flipped; files that were trimmed go in
    # ``just_trimmed`` so they still get pushed when mtimes are equal.
    just_trimmed = set()
    if not args.dry_run:
        for fname, local_path in list(local_jsons.items()):
            try:
                trimmed, before, after = trim_at_last_compact(local_path)
            except (OSError, ValueError):
                continue
            if trimmed:
                just_trimmed.add(fname)
                saved_pct = 100 * (before - after) / before if before else 0
                print(f"{indent}[TRIMMED] {fname} "
                      f"{format_size(before)} → {format_size(after)} "
                      f"(-{saved_pct:.1f}%)")

    if not args.dry_run:
        for fname, local_path in list(local_jsons.items()):
            try:
                n_fixes = repair_uuid_chain(local_path)
            except (OSError, ValueError):
                continue
            if n_fixes:
                print(f"{indent}[REPAIRED] {fname} "
                      f"({n_fixes} broken parentUuid link{'s' if n_fixes > 1 else ''})")

    pushed = pulled = skipped = 0

    # Load/create titles mapping: {session_id: title}
    titles_file = remote_files.get("_titles.json")
    remote_titles = {}
    if titles_file:
        try:
            remote_titles = json.loads(download_string(service, titles_file["id"]))
        except (json.JSONDecodeError, Exception):
            pass
    local_titles = {}
    titles_changed = False

    # Sync files that exist locally
    for fname, local_path in local_jsons.items():
        local_md5 = local_file_md5(local_path)
        local_mtime = local_path.stat().st_mtime
        local_size = local_path.stat().st_size

        if fname in remote_files:
            remote = remote_files[fname]
            if local_md5 == remote.get("md5Checksum", ""):
                skipped += 1
                continue

            remote_mtime = datetime.fromisoformat(
                remote["modifiedTime"].replace("Z", "+00:00")
            ).timestamp()

            remote_size = int(remote.get("size", 0))

            # Try merge when both sides have unique entries (two machines
            # background-syncing the same conversation).  Only attempt when
            # both files are within 2x of each other — if one is drastically
            # smaller, the normal push/pull/shrunk-guard logic is more
            # appropriate than a merge.
            size_ratio_ok = (remote_size > 0 and local_size > 0
                             and local_size <= remote_size * 2
                             and remote_size <= local_size * 2)
            if (not args.dry_run and not args.pull_only and not args.push_only
                    and size_ratio_ok):
                try:
                    remote_text = download_string(service, remote["id"])
                    merged = merge_jsonl_by_timestamp(local_path, remote_text)
                except Exception:
                    merged = False
                if merged:
                    repair_uuid_chain(local_path)
                    if local_cwd:
                        rewrite_cwd_if_needed(local_path, local_cwd)
                    merged_size = local_path.stat().st_size
                    media = MediaFileUpload(str(local_path))
                    service.files().update(fileId=remote["id"], media_body=media).execute()
                    print(f"{indent}[MERGED] {fname} "
                          f"(local {format_size(local_size)} + remote {format_size(remote_size)} → {format_size(merged_size)})")
                    pushed += 1
                    continue

            if local_mtime > remote_mtime and not args.pull_only:
                # Guard: if local is newer but much smaller (<=95% of remote),
                # the local file was likely overwritten/corrupted — pull remote
                # instead.  Skip the guard when:
                #   - file was trimmed in THIS sync cycle (just_trimmed)
                #   - a .pretrim.bak exists (trimmed in a prior cycle)
                #   - user explicitly asked --push (never pull during push)
                has_pretrim_bak = local_path.with_suffix(
                    local_path.suffix + ".pretrim.bak").exists()
                if (remote_size > 0 and local_size <= remote_size * 0.95
                        and fname not in just_trimmed
                        and not has_pretrim_bak
                        and not args.push_only):
                    action = "WOULD PULL (local shrunk)" if args.dry_run else "PULLED (local shrunk)"
                    if not args.dry_run:
                        download_file(service, remote["id"], local_path)
                        trim_at_last_compact(local_path)
                        repair_uuid_chain(local_path)
                        if local_cwd:
                            rewrite_cwd_if_needed(local_path, local_cwd)
                    print(f"{indent}[{action}] {fname} ({format_size(remote_size)} remote > {format_size(local_size)} local)")
                    pulled += 1
                else:
                    action = "WOULD PUSH" if args.dry_run else "PUSHED"
                    if not args.dry_run:
                        media = MediaFileUpload(str(local_path))
                        service.files().update(fileId=remote["id"], media_body=media).execute()
                    print(f"{indent}[{action}] {fname} ({format_size(local_size)}, {format_time(local_mtime)})")
                    pushed += 1
            elif remote_mtime > local_mtime and not args.push_only:
                action = "WOULD PULL" if args.dry_run else "PULLED"
                if not args.dry_run:
                    download_file(service, remote["id"], local_path)
                    trim_at_last_compact(local_path)
                    repair_uuid_chain(local_path)
                print(f"{indent}[{action}] {fname} ({format_size(remote_size)}, {format_time(remote_mtime)})")
                pulled += 1
            elif fname in just_trimmed and not args.pull_only:
                # Trimmed locally but mtimes are equal — push the smaller
                # version so every other machine also picks up the trim.
                action = "WOULD PUSH (trimmed)" if args.dry_run else "PUSHED (trimmed)"
                if not args.dry_run:
                    media = MediaFileUpload(str(local_path))
                    service.files().update(fileId=remote["id"], media_body=media).execute()
                print(f"{indent}[{action}] {fname} ({format_size(local_size)}, {format_time(local_mtime)})")
                pushed += 1
            else:
                skipped += 1
        elif not args.pull_only:
            action = "WOULD PUSH NEW" if args.dry_run else "PUSHED NEW"
            if not args.dry_run:
                media = MediaFileUpload(str(local_path))
                service.files().create(
                    body={"name": fname, "parents": [folder_id]},
                    media_body=media,
                ).execute()
            print(f"{indent}[{action}] {fname} ({format_size(local_size)}, {format_time(local_mtime)})")
            pushed += 1

    # Extract titles from local files we just pushed (or all local files for title sync)
    if not args.pull_only:
        for fname, local_path in local_jsons.items():
            session_id = fname.replace(".jsonl", "")
            title = get_conversation_title(local_path)
            if title:
                if remote_titles.get(session_id) != title:
                    remote_titles[session_id] = title
                    titles_changed = True

    # Pull files that exist only on remote
    if not args.push_only:
        for fname, remote in remote_files.items():
            if fname.startswith("_") or not fname.endswith(".jsonl"):
                continue
            if chat_filters and not any(fname.startswith(c) for c in chat_filters):
                continue
            if fname not in local_jsons:
                remote_size = int(remote.get("size", 0))
                remote_mtime = datetime.fromisoformat(
                    remote["modifiedTime"].replace("Z", "+00:00")
                ).timestamp()
                action = "WOULD PULL NEW" if args.dry_run else "PULLED NEW"
                if not args.dry_run:
                    download_file(service, remote["id"], local_dir / fname)
                    trim_at_last_compact(local_dir / fname)
                    repair_uuid_chain(local_dir / fname)
                    if local_cwd:
                        rewrite_cwd_if_needed(local_dir / fname, local_cwd)
                    # Inject saved title into the downloaded conversation
                    session_id = fname.replace(".jsonl", "")
                    saved_title = remote_titles.get(session_id)
                    if saved_title:
                        inject_custom_title(local_dir / fname, session_id, saved_title)
                print(f"{indent}[{action}] {fname} ({format_size(remote_size)}, {format_time(remote_mtime)})")
                pulled += 1

    # Also inject titles into existing local files that were pulled (updated)
    if not args.push_only and not args.dry_run:
        for fname, local_path in local_jsons.items():
            session_id = fname.replace(".jsonl", "")
            saved_title = remote_titles.get(session_id)
            if saved_title:
                inject_custom_title(local_path, session_id, saved_title)

    # Upload updated titles mapping
    if titles_changed and not args.dry_run:
        upload_string(
            service,
            json.dumps(remote_titles, indent=2),
            "_titles.json",
            folder_id,
            existing_id=titles_file["id"] if titles_file else None,
        )

    return pushed, pulled, skipped


def _get_memory_origin_session(md_path: Path) -> str | None:
    """Extract originSessionId from a memory file's YAML frontmatter."""
    try:
        with open(md_path, "r") as f:
            first_line = f.readline().strip()
            if first_line != "---":
                return None
            for line in f:
                line = line.strip()
                if line == "---":
                    break
                if line.startswith("originSessionId:"):
                    return line.split(":", 1)[1].strip()
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _find_largest_chat(local_jsons: dict, remote_files: dict) -> str | None:
    """Find the largest chat ID from local and remote JSONL files."""
    best_id, best_size = None, 0
    for fname, path in local_jsons.items():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > best_size:
            best_size = size
            best_id = fname.replace(".jsonl", "")
    for fname, info in remote_files.items():
        if not is_jsonl_conversation_file(fname) or fname.startswith("_"):
            continue
        size = int(info.get("size", 0))
        if size > best_size:
            best_size = size
            best_id = fname.replace(".jsonl", "")
    return best_id


def _migrate_flat_memory(service, memory_folder_id, target_chat_folder_id):
    """Move flat memory files (legacy) into a chat-specific subfolder."""
    flat_files = list_remote_files(service, memory_folder_id)
    for fname, info in flat_files.items():
        if not fname.endswith(".md"):
            continue
        service.files().update(
            fileId=info["id"],
            addParents=target_chat_folder_id,
            removeParents=memory_folder_id,
        ).execute()
    return len(flat_files)


def sync_memory(service, folder_id, local_dir: Path, args, indent="    ",
                local_jsons=None, remote_files_map=None):
    """Sync the memory/ subfolder between local_dir and a _memory Drive folder.

    Memory files are organized per-chat on Drive: _memory/<chat_id>/*.md
    Each memory file's originSessionId frontmatter determines which chat it belongs to.
    Files without originSessionId are associated with the largest chat in the repo.
    """
    memory_dir = local_dir / "memory"
    has_local = memory_dir.is_dir() and any(memory_dir.glob("*.md"))

    # Get or check for _memory folder on Drive
    remote_folders = list_drive_folders(service, folder_id)
    memory_folder_id = remote_folders.get("_memory") if "_memory" in remote_folders else None

    if not has_local and not memory_folder_id:
        return 0, 0

    # Create folders as needed
    if has_local and not memory_folder_id and not args.pull_only and not args.dry_run:
        memory_folder_id = get_or_create_folder(service, "_memory", folder_id)
    if memory_folder_id is None and args.pull_only:
        return 0, 0
    if memory_folder_id is None and args.dry_run:
        local_files = list(memory_dir.glob("*.md"))
        if local_files:
            print(f"{indent}[WOULD PUSH] memory/ ({len(local_files)} files)")
        return len(local_files), 0

    # Determine fallback chat ID (largest chat) for files without originSessionId
    fallback_chat_id = _find_largest_chat(
        local_jsons or {}, remote_files_map or {},
    )

    # List chat subfolders inside _memory on Drive
    chat_subfolders = list_drive_folders(service, memory_folder_id)

    # Migrate legacy flat memory files into largest-chat subfolder
    flat_files = list_remote_files(service, memory_folder_id)
    flat_md_files = {k: v for k, v in flat_files.items() if k.endswith(".md")}
    if flat_md_files and fallback_chat_id and not args.dry_run:
        target_id = chat_subfolders.get(fallback_chat_id)
        if not target_id:
            target_id = get_or_create_folder(service, fallback_chat_id, memory_folder_id)
            chat_subfolders[fallback_chat_id] = target_id
        moved = _migrate_flat_memory(service, memory_folder_id, target_id)
        if moved:
            print(f"{indent}[memory] migrated {moved} file(s) to chat {fallback_chat_id[:8]}…")

    # Ensure local memory dir exists for pull
    if not memory_dir.exists() and not args.push_only:
        if not args.dry_run:
            memory_dir.mkdir(parents=True, exist_ok=True)

    pushed = pulled = 0

    # --- Push: group local files by originSessionId, sync to per-chat subfolder ---
    if has_local and not args.pull_only:
        # Group local memory files by chat ID
        by_chat = {}  # chat_id -> [md_path, ...]
        for md_file in sorted(memory_dir.glob("*.md")):
            origin = _get_memory_origin_session(md_file) or fallback_chat_id
            if origin:
                by_chat.setdefault(origin, []).append(md_file)

        for chat_id, md_files in by_chat.items():
            # Get or create chat subfolder in _memory
            chat_folder_id = chat_subfolders.get(chat_id)
            if not chat_folder_id and not args.dry_run:
                chat_folder_id = get_or_create_folder(service, chat_id, memory_folder_id)
                chat_subfolders[chat_id] = chat_folder_id

            remote_chat_files = list_remote_files(service, chat_folder_id) if chat_folder_id else {}

            for md_file in md_files:
                fname = md_file.name
                local_md5 = local_file_md5(md_file)
                local_mtime = md_file.stat().st_mtime

                if fname in remote_chat_files:
                    remote = remote_chat_files[fname]
                    if local_md5 == remote.get("md5Checksum", ""):
                        continue
                    remote_mtime = datetime.fromisoformat(
                        remote["modifiedTime"].replace("Z", "+00:00")
                    ).timestamp()
                    if local_mtime > remote_mtime:
                        if not args.dry_run:
                            media = MediaFileUpload(str(md_file))
                            service.files().update(fileId=remote["id"], media_body=media).execute()
                        pushed += 1
                else:
                    if not args.dry_run:
                        media = MediaFileUpload(str(md_file))
                        service.files().create(
                            body={"name": fname, "parents": [chat_folder_id]},
                            media_body=media,
                        ).execute()
                    pushed += 1

    # --- Pull: iterate chat subfolders, download memory files ---
    if not args.push_only:
        # Refresh chat subfolders (may have been created during push)
        if not chat_subfolders:
            chat_subfolders = list_drive_folders(service, memory_folder_id)
        local_names = {p.name for p in memory_dir.glob("*.md")} if memory_dir.is_dir() else set()

        for chat_id, chat_folder_id in chat_subfolders.items():
            remote_chat_files = list_remote_files(service, chat_folder_id)
            for fname, remote in remote_chat_files.items():
                if not fname.endswith(".md"):
                    continue
                local_path = memory_dir / fname
                if fname in local_names:
                    local_md5 = local_file_md5(local_path)
                    if local_md5 == remote.get("md5Checksum", ""):
                        continue
                    remote_mtime = datetime.fromisoformat(
                        remote["modifiedTime"].replace("Z", "+00:00")
                    ).timestamp()
                    if remote_mtime > local_path.stat().st_mtime:
                        if not args.dry_run:
                            download_file(service, remote["id"], local_path)
                        pulled += 1
                else:
                    if not args.dry_run:
                        download_file(service, remote["id"], local_path)
                    pulled += 1

    if pushed or pulled:
        action_parts = []
        if pushed:
            verb = "would push" if args.dry_run else "pushed"
            action_parts.append(f"{verb} {pushed}")
        if pulled:
            verb = "would pull" if args.dry_run else "pulled"
            action_parts.append(f"{verb} {pulled}")
        print(f"{indent}[memory] {', '.join(action_parts)}")

    return pushed, pulled


def sync_codex_files(service, folder_id, entries: list[dict], args, git_root=None,
                     rel_path=".", indent="    ", remote_files=None,
                     require_remote_prefix=True):
    """Sync Codex rollout JSONL files between local ~/.codex and Drive."""
    if remote_files is None:
        remote_files = list_remote_files(service, folder_id)

    chat_ids = getattr(args, "chat_id", None)
    chat_filters = [c.strip() for c in chat_ids.split(",")] if chat_ids else None
    local_cwd = _codex_local_cwd(git_root, rel_path)
    remote_by_local = {}
    for remote_name, remote in remote_files.items():
        if require_remote_prefix:
            if not is_codex_remote_file(remote_name):
                continue
            local_name = codex_local_name_from_remote(remote_name)
        else:
            if remote_name.startswith("_") or not remote_name.endswith(".jsonl"):
                continue
            local_name = remote_name
        remote_by_local[local_name] = (remote_name, remote)

    by_name = {}
    for entry in entries:
        fname = entry["path"].name
        if chat_filters and not codex_name_matches_chat_filters(
            fname, chat_filters, title=entry.get("title"),
            session_id=entry.get("session_id")
        ):
            continue
        by_name[fname] = entry

    if not args.dry_run and local_cwd:
        for fname, entry in list(by_name.items()):
            n = rewrite_codex_cwd_if_needed(entry["path"], local_cwd)
            if n:
                print(f"{indent}[codex CWD REWRITE] {fname} ({n} entries -> {local_cwd})")

    pushed = pulled = skipped = 0
    pulled_entries = []

    for fname, entry in by_name.items():
        local_path = entry["path"]
        local_md5 = local_file_md5(local_path)
        local_mtime = local_path.stat().st_mtime
        local_size = local_path.stat().st_size

        if fname in remote_by_local:
            remote_name, remote = remote_by_local[fname]
            if local_md5 == remote.get("md5Checksum", ""):
                skipped += 1
                continue
            remote_mtime = datetime.fromisoformat(
                remote["modifiedTime"].replace("Z", "+00:00")
            ).timestamp()
            remote_size = int(remote.get("size", 0))
            if local_mtime > remote_mtime and not args.pull_only:
                if not args.dry_run:
                    media = MediaFileUpload(str(local_path))
                    service.files().update(fileId=remote["id"], media_body=media).execute()
                pushed += 1
            elif remote_mtime > local_mtime and not args.push_only:
                if not args.dry_run:
                    download_file(service, remote["id"], local_path)
                    if local_cwd:
                        rewrite_codex_cwd_if_needed(local_path, local_cwd)
                    pulled_meta = parse_codex_rollout(local_path)
                    pulled_meta.update({
                        "path": local_path,
                        "title": entry.get("title"),
                        "session_id": pulled_meta.get("session_id") or entry.get("session_id"),
                    })
                    pulled_entries.append(pulled_meta)
                pulled += 1
            else:
                skipped += 1
        elif not args.pull_only:
            remote_name = codex_remote_name_from_local(fname) if require_remote_prefix else fname
            if not args.dry_run:
                media = MediaFileUpload(str(local_path))
                service.files().create(
                    body={"name": remote_name, "parents": [folder_id]},
                    media_body=media,
                ).execute()
            pushed += 1

    if not args.push_only:
        for local_name, (remote_name, remote) in remote_by_local.items():
            sid_match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', local_name)
            remote_sid = sid_match.group(1) if sid_match else None
            if chat_filters and not codex_name_matches_chat_filters(
                remote_name, chat_filters, session_id=remote_sid
            ):
                continue
            if local_name in by_name:
                continue
            local_path = _codex_rollout_path_for_remote_name(local_name)
            remote_size = int(remote.get("size", 0))
            remote_mtime = datetime.fromisoformat(
                remote["modifiedTime"].replace("Z", "+00:00")
            ).timestamp()
            if not args.dry_run:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                download_file(service, remote["id"], local_path)
                if local_cwd:
                    rewrite_codex_cwd_if_needed(local_path, local_cwd)
                pulled_meta = parse_codex_rollout(local_path)
                pulled_meta.update({"path": local_path})
                pulled_entries.append(pulled_meta)
            pulled += 1

    if not args.dry_run:
        upsert_codex_session_index(pulled_entries)

    return pushed, pulled, skipped


def sync_codex_push(service, root_folder_id, codex_git_projects: dict, args) -> bool:
    """Push/sync local Codex conversations under repo Codex subfolders."""
    printed_any = False
    if args.pull_only:
        return printed_any

    for url_key, entries in sorted(codex_git_projects.items()):
        raw_url = next((e.get("git_url") for e in entries if e.get("git_url")), url_key)
        if not repo_matches_filter(raw_url, args.repo):
            continue
        repo_folder_id = get_or_create_folder(service, url_key, root_folder_id)
        if not args.dry_run:
            service.files().update(fileId=repo_folder_id, body={"description": raw_url}).execute()
            repo_level_files = list_remote_files(service, repo_folder_id)
            upload_string(
                service,
                json.dumps({
                    "remote_url": raw_url,
                    "normalized_key": url_key,
                    "sources": ["claude", "codex"],
                }, indent=2),
                "_metadata.json",
                repo_folder_id,
                existing_id=repo_level_files.get("_metadata.json", {}).get("id"),
            )

        by_rel = {}
        for entry in entries:
            by_rel.setdefault(entry.get("rel_path") or ".", []).append(entry)
        existing_subs = list_drive_folders(service, repo_folder_id)

        B = "  ╠═══════════════════════════════════════════════════════════════════════════"
        if not printed_any:
            print(B)
            printed_any = True
        git_root = next((e.get("git_root") for e in entries if e.get("git_root")), None)
        print(f"  ║ [codex] {raw_url}")
        if git_root:
            print(f"  ║   ╰─> {git_root}")
        print(f"  ║ ----------------------------------------------------------------------")

        chat_ids = getattr(args, "chat_id", None)
        chat_filters = [c.strip() for c in chat_ids.split(",")] if chat_ids else None

        for rel_path, rel_entries in sorted(by_rel.items()):
            sf_name = codex_rel_path_to_drive_subfolder(rel_path)
            subfolder_id = existing_subs.get(sf_name)
            if not subfolder_id:
                subfolder_id = get_or_create_folder(service, sf_name, repo_folder_id)
            remote_sub_files = list_remote_files(service, subfolder_id)
            filtered_entries = [
                e for e in rel_entries
                if not chat_filters or codex_name_matches_chat_filters(
                    e["path"].name, chat_filters, title=e.get("title"),
                    session_id=e.get("session_id")
                )
            ]
            local_count = len(filtered_entries)
            local_size = sum(e["path"].stat().st_size for e in filtered_entries)
            remote_count = sum(1 for k in remote_sub_files if is_codex_remote_file(k))
            remote_size = sum(
                int(f.get("size", 0)) for k, f in remote_sub_files.items()
                if is_codex_remote_file(k)
            )
            if not local_count and not remote_count:
                continue
            label = "." if rel_path == "." else rel_path
            print(f"  ║ [codex] {label:<27s} {local_count:>2} local ({format_size(local_size):>8})  {remote_count:>2} remote ({format_size(remote_size):>8})")
            groups = {}
            for entry in filtered_entries:
                slug = codex_group_slug(entry.get("title"))
                groups.setdefault(slug, []).append(entry)
            for slug, group_entries in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                g_size = sum(e["path"].stat().st_size for e in group_entries)
                g_title = group_entries[0].get("title") or "(untitled)"
                g_title_short = g_title.strip().split("\n")[0][:40]
                if len(group_entries) == 1:
                    sid = group_entries[0].get("session_id", "")[:8]
                    print(f"  ║   ╰─ {sid}  \"{g_title_short}\" ({format_size(g_size)})")
                else:
                    print(f"  ║   ╰─ [codex group] \"{g_title_short}\" ({len(group_entries)} rollouts, {format_size(g_size)})")
                    for entry in sorted(group_entries, key=lambda e: e.get("session_id", "")):
                        sid = entry.get("session_id", "")[:8]
                        sz = format_size(entry["path"].stat().st_size)
                        print(f"  ║      ╰─ {sid}  {sz}")
            pushed, pulled, skipped = sync_codex_files(
                service, subfolder_id, rel_entries, args,
                git_root=rel_entries[0].get("git_root"),
                rel_path=rel_path,
                indent="  ║   ",
                remote_files=remote_sub_files,
            )
            if pushed or pulled:
                if args.dry_run:
                    print(f"  ║   => codex would push {pushed}, would pull {pulled}, {skipped} unchanged")
                else:
                    print(f"  ║   => codex {pushed} pushed, {pulled} pulled, {skipped} unchanged")
        print(B)
    return printed_any


def sync_codex_pull(service, root_folder_id, codex_git_projects: dict, args,
                    printed_any=False) -> bool:
    """Pull remote Codex conversations for cloned repos, including new sessions."""
    if args.push_only:
        return printed_any
    remote_repo_folders = list_drive_folders(service, root_folder_id, include_description=True)
    local_repos = scan_local_git_repos()

    local_by_url = {}
    for url_key, entries in codex_git_projects.items():
        for entry in entries:
            rel_path = entry.get("rel_path") or "."
            local_by_url.setdefault(url_key, {}).setdefault(rel_path, []).append(entry)

    for url_key, folder_info in sorted(remote_repo_folders.items()):
        repo_fid = folder_info["id"]
        raw_url = folder_info.get("description") or url_key
        if not repo_matches_filter(raw_url, args.repo):
            continue
        remote_subfolders = list_drive_folders(service, repo_fid)
        repo_match = local_repos.get(url_key)
        git_root = repo_match[0] if repo_match else None
        raw_url = repo_match[1] if repo_match else raw_url
        if not git_root and url_key in local_by_url:
            git_root = next(
                (e.get("git_root") for entries in local_by_url[url_key].values()
                 for e in entries if e.get("git_root")),
                None,
            )

        codex_records = []
        seen_rel_paths = set()
        for subfolder_name, subfolder_id in sorted(remote_subfolders.items()):
            legacy_codex_folder = is_codex_drive_subfolder(subfolder_name)
            rel_path = (
                codex_drive_subfolder_to_rel_path(subfolder_name)
                if legacy_codex_folder
                else drive_subfolder_to_rel_path(subfolder_name)
            )
            if git_root and rel_path != "." and is_gitignored(git_root, rel_path):
                continue
            remote_sub_files = list_remote_files(service, subfolder_id)
            if legacy_codex_folder:
                codex_remote_files = {
                    k: v for k, v in remote_sub_files.items()
                    if not k.startswith("_") and k.endswith(".jsonl")
                }
            else:
                codex_remote_files = {
                    k: v for k, v in remote_sub_files.items()
                    if is_codex_remote_file(k)
                }
            entries = local_by_url.get(url_key, {}).get(rel_path, []) if rel_path not in seen_rel_paths else []
            if codex_remote_files or entries:
                seen_rel_paths.add(rel_path)
                codex_records.append((
                    subfolder_id,
                    legacy_codex_folder,
                    rel_path,
                    remote_sub_files,
                    codex_remote_files,
                    entries,
                ))
        has_any_remote = any(rec[4] for rec in codex_records)
        if not codex_records or not has_any_remote:
            continue

        B = "  ╠═══════════════════════════════════════════════════════════════════════════"

        if not git_root:
            if not printed_any:
                print(B)
                printed_any = True
            print(f"  ║ [codex] {raw_url}  (no local clone)")
            print(f"  ║ ----------------------------------------------------------------------")
            print(B)
            continue

        chat_ids = getattr(args, "chat_id", None)
        chat_filters = [c.strip() for c in chat_ids.split(",")] if chat_ids else None

        any_activity = False
        record_results = []
        for (subfolder_id, legacy_codex_folder, rel_path, remote_sub_files,
             codex_remote_files, entries) in codex_records:
            remote_count = len(codex_remote_files)
            if not remote_count and not entries:
                continue
            pushed, pulled, skipped = sync_codex_files(
                service, subfolder_id, entries, args,
                git_root=git_root,
                rel_path=rel_path,
                indent="  ║   ",
                remote_files=remote_sub_files,
                require_remote_prefix=not legacy_codex_folder,
            )
            if pushed or pulled or remote_count:
                any_activity = True
            record_results.append((rel_path, entries, codex_remote_files, pushed, pulled, skipped))

        if not any_activity:
            continue

        if not printed_any:
            print(B)
            printed_any = True
        print(f"  ║ [codex] {raw_url}")
        print(f"  ║   ╰─> {git_root}")
        print(f"  ║ ----------------------------------------------------------------------")

        for (rel_path, entries, codex_remote_files, pushed, pulled, skipped) in record_results:
            remote_count = len(codex_remote_files)
            remote_size = sum(int(f.get("size", 0)) for f in codex_remote_files.values())
            filtered_entries = [
                e for e in entries
                if not chat_filters or codex_name_matches_chat_filters(
                    e["path"].name, chat_filters, title=e.get("title"),
                    session_id=e.get("session_id")
                )
            ]
            local_count = len(filtered_entries)
            local_size = sum(e["path"].stat().st_size for e in filtered_entries)
            if not local_count and not remote_count:
                continue
            label = "." if rel_path == "." else rel_path
            print(f"  ║ [codex] {label:<27s} {local_count:>2} local ({format_size(local_size):>8})  {remote_count:>2} remote ({format_size(remote_size):>8})")
            if filtered_entries:
                groups = {}
                for entry in filtered_entries:
                    slug = codex_group_slug(entry.get("title"))
                    groups.setdefault(slug, []).append(entry)
                for slug, group_entries in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                    g_size = sum(e["path"].stat().st_size for e in group_entries)
                    g_title = group_entries[0].get("title") or "(untitled)"
                    g_title_short = g_title.strip().split("\n")[0][:40]
                    if len(group_entries) == 1:
                        sid = group_entries[0].get("session_id", "")[:8]
                        print(f"  ║   ╰─ {sid}  \"{g_title_short}\" ({format_size(g_size)})")
                    else:
                        print(f"  ║   ╰─ [codex group] \"{g_title_short}\" ({len(group_entries)} rollouts, {format_size(g_size)})")
                        for entry in sorted(group_entries, key=lambda e: e.get("session_id", "")):
                            sid = entry.get("session_id", "")[:8]
                            sz = format_size(entry["path"].stat().st_size)
                            print(f"  ║      ╰─ {sid}  {sz}")
            if pushed or pulled:
                if args.dry_run:
                    print(f"  ║   => codex would push {pushed}, would pull {pulled}, {skipped} unchanged")
                else:
                    print(f"  ║   => codex {pushed} pushed, {pulled} pulled, {skipped} unchanged")
        print(B)
    return printed_any


def build_board_rows() -> list[dict]:
    """Collect local Claude and Codex conversations for the visualization board."""
    rows = []
    claude_index = build_local_index()
    if None in claude_index:
        resolve_unmatched_projects(claude_index)
    for url_key, entries in claude_index.items():
        if url_key is None:
            continue
        for project_dir, raw_url, git_root, rel_path in entries:
            for p in sorted(project_dir.glob("*.jsonl")):
                if is_empty_conversation(p):
                    continue
                rows.append({
                    "source": "claude",
                    "prefix": "[claude]",
                    "repo": raw_url or url_key,
                    "rel_path": rel_path or ".",
                    "session_id": p.stem,
                    "title": get_conversation_title(p) or "",
                    "path": str(p),
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                })
    codex_index = build_codex_index()
    for url_key, entries in codex_index.items():
        if url_key is None:
            continue
        for entry in entries:
            p = entry["path"]
            rows.append({
                "source": "codex",
                "prefix": "[codex]",
                "repo": entry.get("git_url") or url_key,
                "rel_path": entry.get("rel_path") or ".",
                "session_id": entry.get("session_id") or p.stem,
                "title": entry.get("title") or "",
                "path": str(p),
                "size": p.stat().st_size,
                "mtime": entry.get("updated_at") or p.stat().st_mtime,
            })
    return sorted(rows, key=lambda r: r["mtime"], reverse=True)


def generate_board(output_path: Path) -> Path:
    rows = build_board_rows()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generated = html.escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    body_rows = []
    for row in rows:
        title = row["title"] or "(untitled)"
        body_rows.append(
            "<tr>"
            f"<td class=\"source {row['source']}\">{html.escape(row['prefix'])}</td>"
            f"<td>{html.escape(title)}</td>"
            f"<td>{html.escape(row['repo'])}</td>"
            f"<td>{html.escape(row['rel_path'])}</td>"
            f"<td><code>{html.escape(str(row['session_id'])[:12])}</code></td>"
            f"<td>{html.escape(format_size(row['size']))}</td>"
            f"<td>{html.escape(format_time(row['mtime']))}</td>"
            f"<td><code>{html.escape(row['path'])}</code></td>"
            "</tr>"
        )
    output_path.write_text(f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Claude/Codex Conversation Board</title>
  <style>
    body {{ font: 14px/1.4 system-ui, sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }}
    h1 {{ margin: 0 0 4px; font-size: 24px; }}
    .meta {{ color: #64748b; margin-bottom: 18px; }}
    table {{ border-collapse: collapse; width: 100%; background: white; border: 1px solid #e2e8f0; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e2e8f0; text-align: left; vertical-align: top; }}
    th {{ background: #eef2f7; color: #334155; position: sticky; top: 0; }}
    tr:hover {{ background: #f8fafc; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .source {{ font-weight: 700; white-space: nowrap; }}
    .claude {{ color: #9a3412; }}
    .codex {{ color: #075985; }}
  </style>
</head>
<body>
  <h1>Conversation Board</h1>
  <div class="meta">{len(rows)} conversations, generated {generated}</div>
  <table>
    <thead>
      <tr><th>Source</th><th>Title</th><th>Repo</th><th>Path</th><th>Session</th><th>Size</th><th>Updated</th><th>Local file</th></tr>
    </thead>
    <tbody>
      {''.join(body_rows)}
    </tbody>
  </table>
</body>
</html>
""")
    return output_path


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def run_sync(args, service, root_folder_id):
    """Run one sync cycle. Returns True if any changes were made."""
    # Build local index: git_url_key -> [(project_dir, raw_git_url), ...]
    local_index = build_local_index()
    codex_index = build_codex_index()

    # Resolve unmatched projects (from other machines) by scanning sibling repos
    if None in local_index:
        resolve_unmatched_projects(local_index)

    # Clean up empty local conversations (no assistant reply)
    cleaned = 0
    for entries in local_index.values():
        for project_dir, _, _, _ in entries:
            for jsonl in project_dir.glob("*.jsonl"):
                if is_empty_conversation(jsonl):
                    if not args.dry_run:
                        jsonl.unlink(missing_ok=True)
                        # Also remove companion dir if it exists
                        companion = jsonl.with_suffix("")
                        if companion.is_dir():
                            import shutil
                            shutil.rmtree(companion)
                    cleaned += 1
    if cleaned:
        verb = "Would clean" if args.dry_run else "Cleaned"
        print(f"{verb} {cleaned} empty conversation(s)")

    git_projects = {k: v for k, v in local_index.items() if k is not None}
    no_git = local_index.get(None, [])
    codex_git_projects = {k: v for k, v in codex_index.items() if k is not None}
    codex_no_git = codex_index.get(None, [])

    print(f"Found {sum(len(v) for v in git_projects.values())} projects with git remotes, "
          f"{len(no_git)} without; "
          f"{sum(len(v) for v in codex_git_projects.values())} Codex conversations with git remotes, "
          f"{len(codex_no_git)} Codex without")

    # --- DELETE LOCAL: remove local conversation files ---
    if args.delete and args.local:
        chat_filters = [c.strip() for c in args.chat_id.split(",")] if args.chat_id else None
        to_delete = []
        # Search git projects
        for url_key, entries in git_projects.items():
            raw_url = entries[0][1]
            if not repo_matches_filter(raw_url, args.repo):
                continue
            for project_dir, _, git_root, rel_path in entries:
                for jsonl_path in sorted(project_dir.glob("*.jsonl")):
                    if chat_filters and not any(jsonl_path.name.startswith(c) for c in chat_filters):
                        continue
                    title = get_conversation_title(jsonl_path)
                    title_str = f'"{title}"' if title else "(untitled)"
                    size = format_size(jsonl_path.stat().st_size)
                    to_delete.append((jsonl_path, title_str, size))
        # Also search no-git projects (only when filtering by --chat)
        if chat_filters and not args.repo:
            for project_dir, _, _, _ in no_git:
                for jsonl_path in sorted(project_dir.glob("*.jsonl")):
                    if not any(jsonl_path.name.startswith(c) for c in chat_filters):
                        continue
                    title = get_conversation_title(jsonl_path)
                    title_str = f'"{title}"' if title else "(untitled)"
                    size = format_size(jsonl_path.stat().st_size)
                    to_delete.append((jsonl_path, title_str, size))

        if not to_delete:
            print("No matching local conversations to delete.")
            return True

        # Require confirmation for repo-wide delete (no --chat)
        if not chat_filters:
            print(f"About to delete {len(to_delete)} local conversations:")
            for jsonl_path, title_str, size in to_delete:
                print(f"  {jsonl_path.stem[:8]}…  {title_str}  ({size})")
            if not args.dry_run:
                confirm = input("Type 'yes' to confirm: ").strip()
                if confirm != "yes":
                    print("Aborted.")
                    return True

        for jsonl_path, title_str, size in to_delete:
            if args.dry_run:
                print(f"  [WOULD DELETE] {jsonl_path.stem[:8]}…  {title_str}  ({size})")
            else:
                jsonl_path.unlink()
                print(f"  [DELETED] {jsonl_path.stem[:8]}…  {title_str}  ({size})")

        print("Done.")
        return True

    # --- DELETE REMOTE: remove conversations from Drive ---
    if args.delete:
        chat_filters = [c.strip() for c in args.chat_id.split(",")] if args.chat_id else None
        remote_repo_folders = list_drive_folders(service, root_folder_id)
        for url_key, folder_id_val in remote_repo_folders.items():
            folder_id = folder_id_val["id"] if isinstance(folder_id_val, dict) else folder_id_val
            repo_files = list_remote_files(service, folder_id)
            meta_file = repo_files.get("_metadata.json")
            raw_url = url_key
            if meta_file:
                meta = json.loads(download_string(service, meta_file["id"]))
                raw_url = meta.get("remote_url", url_key)
            if not repo_matches_filter(raw_url, args.repo):
                continue

            subfolders = list_drive_folders(service, folder_id)
            to_delete = []
            for sub_name, sub_id in subfolders.items():
                sub_files = list_remote_files(service, sub_id)
                for fname, finfo in sub_files.items():
                    if not fname.endswith(".jsonl"):
                        continue
                    sid_match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', fname)
                    fname_sid = sid_match.group(1) if sid_match else None
                    if chat_filters and not (
                        any(fname.startswith(c) for c in chat_filters)
                        or (fname_sid and any(fname_sid.startswith(c) for c in chat_filters))
                        or codex_name_matches_chat_filters(fname, chat_filters, session_id=fname_sid)
                    ):
                        continue
                    to_delete.append((raw_url, sub_name, fname, finfo["id"]))

            if not to_delete:
                print(f"No matching conversations to delete for {raw_url}")
                continue

            # Require confirmation for repo-wide delete (no --chat)
            if not chat_filters:
                print(f"About to delete {len(to_delete)} conversations from {raw_url}:")
                for _, sub, fname, _ in to_delete:
                    print(f"  {sub}/{fname}")
                if not args.dry_run:
                    confirm = input("Type 'yes' to confirm: ").strip()
                    if confirm != "yes":
                        print("Aborted.")
                        continue

            for _, sub, fname, file_id in to_delete:
                if args.dry_run:
                    print(f"  [WOULD DELETE] {sub}/{fname}")
                else:
                    service.files().delete(fileId=file_id).execute()
                    print(f"  [DELETED] {sub}/{fname}")

        print("Done.")
        return True

    # --- PUSH: upload organized by git remote + relative path subfolder ---
    # Drive structure:
    #   claude-code-history/
    #     github.com__org__repo/
    #       _metadata.json
    #       _root/                    # conversations at repo root
    #         abc.jsonl
    #       flash_attn__cute/         # conversations in flash_attn/cute/
    #         def.jsonl
    push_printed_any = False
    if not args.pull_only:
        # Pre-fetch: ensure all repo folders exist and batch-list their subfolders
        push_repos = []  # (url_key, entries, repo_folder_id)
        for url_key, entries in sorted(git_projects.items()):
            raw_url = entries[0][1]
            if not repo_matches_filter(raw_url, args.repo):
                continue
            repo_folder_id = get_or_create_folder(service, url_key, root_folder_id)
            push_repos.append((url_key, entries, repo_folder_id))

        # Batch: list all repo subfolders + ensure subfolder existence
        repo_fids = {url_key: rfid for url_key, _, rfid in push_repos}
        all_repo_subfolders = batch_list_drive_folders(service, repo_fids) if repo_fids else {}

        # Ensure all subfolders exist and collect their IDs
        sf_ids = {}  # (url_key, subfolder_name) -> folder_id
        for url_key, entries, repo_folder_id in push_repos:
            existing_subs = all_repo_subfolders.get(url_key, {})
            for _, _, _, rel_path in entries:
                sf_name = rel_path_to_drive_subfolder(rel_path)
                if sf_name in existing_subs:
                    sf_ids[(url_key, sf_name)] = existing_subs[sf_name]
                else:
                    sf_ids[(url_key, sf_name)] = get_or_create_folder(
                        service, sf_name, repo_folder_id)

        # Batch: list all remote files in all subfolders
        sf_lookup = {f"{uk}/{sn}": fid for (uk, sn), fid in sf_ids.items()}
        all_sf_files = batch_list_remote_files(service, sf_lookup) if sf_lookup else {}

        for url_key, entries, repo_folder_id in push_repos:
            raw_url = entries[0][1]

            # Store raw URL in folder description for fast lookup during pull
            if not args.dry_run:
                service.files().update(
                    fileId=repo_folder_id,
                    body={"description": raw_url},
                ).execute()

            # Update metadata
            meta = {
                "remote_url": raw_url,
                "normalized_key": url_key,
                "subfolders": [
                    {"rel_path": rel, "local_project_dir": str(d)}
                    for d, _, _, rel in entries
                ],
            }
            # Use pre-fetched file list for the first subfolder to find _metadata.json
            first_sf_name = rel_path_to_drive_subfolder(entries[0][3])
            repo_files = all_sf_files.get(f"{url_key}/{first_sf_name}", {})
            if not args.dry_run:
                # Need repo-level files for _metadata.json — check if already fetched
                repo_level_files = list_remote_files(service, repo_folder_id)
                upload_string(
                    service,
                    json.dumps(meta, indent=2),
                    "_metadata.json",
                    repo_folder_id,
                    existing_id=repo_level_files.get("_metadata.json", {}).get("id"),
                )

            B = "  ╠═══════════════════════════════════════════════════════════════════════════"
            git_root = entries[0][2]
            repo_header_printed = False
            for project_dir, _, _, rel_path in entries:
                subfolder_name = rel_path_to_drive_subfolder(rel_path)
                subfolder_id = sf_ids[(url_key, subfolder_name)]

                local_jsons = {p.name: p for p in sorted(project_dir.glob("*.jsonl"))
                               if not is_empty_conversation(p)}
                local_count = len(local_jsons)
                local_size = sum(p.stat().st_size for p in local_jsons.values())
                remote_sub_files = all_sf_files.get(f"{url_key}/{subfolder_name}", {})
                remote_count = sum(1 for k in remote_sub_files if is_jsonl_conversation_file(k))
                remote_size = sum(
                    int(f.get("size", 0)) for k, f in remote_sub_files.items()
                    if is_jsonl_conversation_file(k)
                )

                subdir_label = "." if rel_path == "." else rel_path
                if not local_count and not remote_count:
                    continue
                if not repo_header_printed:
                    if not push_printed_any:
                        print(B)
                        push_printed_any = True
                    print(f"  ║ {raw_url}")
                    print(f"  ║   ╰─> {git_root}")
                    print(f"  ║ ----------------------------------------------------------------------")
                    repo_header_printed = True
                print(f"  ║ {subdir_label:<35s} {local_count:>2} local ({format_size(local_size):>8})  {remote_count:>2} remote ({format_size(remote_size):>8})")

                if args.verbose:
                    for jsonl_path in sorted(local_jsons.values()):
                        sid = jsonl_path.stem
                        title = get_conversation_title(jsonl_path)
                        size = format_size(jsonl_path.stat().st_size)
                        mtime = format_time(jsonl_path.stat().st_mtime)
                        title_str = f'"{title}"' if title else "(untitled)"
                        print(f"  ║   ╰─ {sid[:8]}…  {title_str:<30s} {size:>8}  {mtime}")

                pushed, pulled, skipped = sync_files(
                    service, subfolder_id, project_dir, args, indent="  ║   ",
                    remote_files=remote_sub_files, local_jsons=local_jsons,
                )
                # Sync memory (skip when filtering by specific chats)
                if not getattr(args, "chat_id", None):
                    mem_pushed, mem_pulled = sync_memory(
                        service, subfolder_id, project_dir, args, indent="  ║   ",
                        local_jsons=local_jsons, remote_files_map=remote_sub_files,
                    )
                if pushed or pulled:
                    if args.dry_run:
                        print(f"  ║   => would push {pushed}, would pull {pulled}, {skipped} unchanged")
                    else:
                        print(f"  ║   => {pushed} pushed, {pulled} pulled, {skipped} unchanged")
            if repo_header_printed:
                print(B)

        push_printed_any = sync_codex_push(
            service, root_folder_id, codex_git_projects, args
        ) or push_printed_any

    # --- PULL: download from remote into matching local project dirs ---
    if not args.push_only:
        remote_repo_folders = list_drive_folders(service, root_folder_id, include_description=True)

        # Build reverse index: for each local git root, map rel_path -> project_dir
        # so we can match remote subfolders to local dirs
        local_by_url = {}  # url_key -> {rel_path: (project_dir, git_root)}
        for url_key, entries in git_projects.items():
            for project_dir, _, git_root, rel_path in entries:
                local_by_url.setdefault(url_key, {})[rel_path] = (
                    project_dir, git_root
                )

        # Filter to repos we need to process in the pull phase,
        # resolving raw URLs from folder description (fast) or metadata file (fallback)
        pull_repos = []
        repo_meta = {}  # url_key -> raw_url
        repos_needing_meta = []  # repos where we need to download _metadata.json
        for url_key, folder_info in sorted(remote_repo_folders.items()):
            if url_key in git_projects and not args.pull_only:
                continue
            repo_fid = folder_info["id"]
            desc = folder_info.get("description", "")
            if desc:
                repo_meta[url_key] = desc
            else:
                repos_needing_meta.append((url_key, repo_fid))
            pull_repos.append((url_key, repo_fid))

        # Batch-fetch repo-level files only for repos without description (need _metadata.json)
        if repos_needing_meta:
            repo_level_files = batch_list_remote_files(
                service, {uk: fid for uk, fid in repos_needing_meta}
            )
            # Batch to update folder descriptions after resolving metadata
            desc_batch = service.new_batch_http_request() if not args.dry_run else None
            desc_count = 0
            for url_key, repo_fid in repos_needing_meta:
                files = repo_level_files.get(url_key, {})
                meta_file = files.get("_metadata.json")
                raw_url = url_key
                if meta_file:
                    try:
                        meta = json.loads(download_string(service, meta_file["id"]))
                        raw_url = meta.get("remote_url", url_key)
                    except Exception:
                        pass
                repo_meta[url_key] = raw_url
                # Backfill folder description for future fast lookup
                if raw_url != url_key and desc_batch is not None:
                    desc_batch.add(service.files().update(
                        fileId=repo_fid, body={"description": raw_url}
                    ))
                    desc_count += 1
            if desc_count > 0:
                desc_batch.execute()

        repos_to_list = {}
        for url_key, repo_fid in pull_repos:
            raw_url = repo_meta.get(url_key, url_key)
            if repo_matches_filter(raw_url, args.repo):
                repos_to_list[url_key] = repo_fid

        # Batch-fetch subfolders for all filtered repos
        all_subfolders = batch_list_drive_folders(service, repos_to_list)

        # Batch-fetch all subfolder file lists in one batch call
        sf_lookup = {}  # (url_key, sf_name) -> sf_id
        for url_key, sfs in all_subfolders.items():
            for sf_name, sf_id in sfs.items():
                sf_lookup[(url_key, sf_name)] = sf_id
        sf_files_all = batch_list_remote_files(service, sf_lookup) if sf_lookup else {}

        pull_printed_first = push_printed_any
        local_repos = scan_local_git_repos()
        for url_key, repo_fid in pull_repos:
            raw_url = repo_meta.get(url_key, url_key)
            remote_subfolders = all_subfolders.get(url_key)

            if not repo_matches_filter(raw_url, args.repo):
                continue

            if remote_subfolders is None:
                continue

            if url_key not in local_by_url:
                # Try to find local git clone to auto-create Claude project dir
                found_root = None
                if url_key in local_repos:
                    found_root = local_repos[url_key][0]
                if found_root:
                    # Populate local_by_url with git_root only (no project_dir yet)
                    # so pull_git_root is set and subfolder sync auto-creates project dirs
                    local_by_url[url_key] = {".": (None, found_root)}
                else:
                    total_convos = 0
                    total_size = 0
                    for sf_name in remote_subfolders:
                        sf_files = sf_files_all.get((url_key, sf_name), {})
                        total_convos += sum(1 for k in sf_files if is_jsonl_conversation_file(k))
                        total_size += sum(
                            int(f.get("size", 0)) for k, f in sf_files.items()
                            if is_jsonl_conversation_file(k)
                        )
                    B = "  ╠═══════════════════════════════════════════════════════════════════════════"
                    if not pull_printed_first:
                        print(B)
                        pull_printed_first = True
                    print(f"  ║ {raw_url}  (no local clone)")
                    print(f"  ║ ----------------------------------------------------------------------")
                    print(f"  ║ {'.':<35s} {total_convos:>2} remote ({format_size(total_size):>8})")
                    print(B)
                    continue

            local_map = local_by_url[url_key]

            B = "  ╠═══════════════════════════════════════════════════════════════════════════"
            pull_git_root = None
            for rp, (pd, gr) in local_map.items():
                pull_git_root = gr
                break
            repo_header_printed = False
            chat_filters = [c.strip() for c in args.chat_id.split(",")] if getattr(args, "chat_id", None) else None
            for subfolder_name, subfolder_id in remote_subfolders.items():
                if is_codex_drive_subfolder(subfolder_name):
                    continue
                rel_path = drive_subfolder_to_rel_path(subfolder_name)

                remote_sub_files = sf_files_all.get((url_key, subfolder_name), {})

                # If --chat specified, skip subfolders with no matching conversations
                if chat_filters:
                    has_match = any(
                        any(fname.startswith(c) for c in chat_filters)
                        for fname in remote_sub_files if is_jsonl_conversation_file(fname)
                    )
                    if not has_match:
                        continue

                remote_count = sum(1 for k in remote_sub_files if is_jsonl_conversation_file(k))
                remote_size = sum(
                    int(f.get("size", 0)) for k, f in remote_sub_files.items()
                    if is_jsonl_conversation_file(k)
                )

                subdir_label = "." if rel_path == "." else rel_path

                if rel_path in local_map and local_map[rel_path][0] is not None:
                    project_dir, _ = local_map[rel_path]
                    local_jsons = {p.name: p for p in sorted(project_dir.glob("*.jsonl"))
                                   if not is_empty_conversation(p)}
                    local_count = len(local_jsons)
                    local_size = sum(p.stat().st_size for p in local_jsons.values())

                    if not local_count and not remote_count:
                        continue
                    if not repo_header_printed:
                        if not pull_printed_first:
                            print(B)
                            pull_printed_first = True
                        print(f"  ║ {raw_url}")
                        if pull_git_root:
                            print(f"  ║   ╰─> {pull_git_root}")
                        print(f"  ║ ----------------------------------------------------------------------")
                        repo_header_printed = True
                    print(f"  ║ {subdir_label:<35s} {local_count:>2} local ({format_size(local_size):>8})  {remote_count:>2} remote ({format_size(remote_size):>8})")

                    if args.verbose:
                        for jsonl_path in sorted(local_jsons.values()):
                            sid = jsonl_path.stem
                            title = get_conversation_title(jsonl_path)
                            size = format_size(jsonl_path.stat().st_size)
                            mtime = format_time(jsonl_path.stat().st_mtime)
                            title_str = f'"{title}"' if title else "(untitled)"
                            print(f"  ║   ╰─ {sid[:8]}…  {title_str:<30s} {size:>8}  {mtime}")

                    pushed, pulled, skipped = sync_files(
                        service, subfolder_id, project_dir, args, indent="  ║   ",
                        remote_files=remote_sub_files, local_jsons=local_jsons,
                    )
                    if not getattr(args, "chat_id", None):
                        mem_pushed, mem_pulled = sync_memory(
                            service, subfolder_id, project_dir, args, indent="  ║   ",
                            local_jsons=local_jsons, remote_files_map=remote_sub_files,
                        )
                    if pushed or pulled:
                        if args.dry_run:
                            print(f"  ║   => would push {pushed}, would pull {pulled}, {skipped} unchanged")
                        else:
                            print(f"  ║   => {pushed} pushed, {pulled} pulled, {skipped} unchanged")
                else:
                    # Git root is known — create the Claude project dir for this subfolder
                    # Skip gitignored paths
                    if pull_git_root and rel_path != "." and is_gitignored(pull_git_root, rel_path):
                        continue
                    if pull_git_root and remote_count > 0:
                        if rel_path == ".":
                            full_path = pull_git_root
                        else:
                            full_path = os.path.join(pull_git_root, rel_path)
                        # Claude encodes /foo/bar_baz as -foo-bar-baz
                        encoded = full_path.replace("/", "-").replace("_", "-").replace(".", "-")
                        project_dir = CLAUDE_PROJECTS_DIR / encoded
                        if not args.dry_run:
                            project_dir.mkdir(parents=True, exist_ok=True)
                        local_jsons = {}
                        local_count = 0
                        local_size = 0
                        if not repo_header_printed:
                            if not pull_printed_first:
                                print(B)
                                pull_printed_first = True
                            print(f"  ║ {raw_url}")
                            if pull_git_root:
                                print(f"  ║   ╰─> {pull_git_root}")
                            print(f"  ║ ----------------------------------------------------------------------")
                            repo_header_printed = True
                        print(f"  ║ {subdir_label:<35s} {local_count:>2} local ({format_size(local_size):>8})  {remote_count:>2} remote ({format_size(remote_size):>8})  (created)")

                        pushed, pulled, skipped = sync_files(
                            service, subfolder_id, project_dir, args, indent="  ║   ",
                            remote_files=remote_sub_files, local_jsons=local_jsons,
                        )
                        if not getattr(args, "chat_id", None):
                            mem_pushed, mem_pulled = sync_memory(
                                service, subfolder_id, project_dir, args, indent="  ║   ",
                                local_jsons=local_jsons, remote_files_map=remote_sub_files,
                            )
                        if pushed or pulled:
                            if args.dry_run:
                                print(f"  ║   => would push {pushed}, would pull {pulled}, {skipped} unchanged")
                            else:
                                print(f"  ║   => {pushed} pushed, {pulled} pulled, {skipped} unchanged")
                    elif remote_count > 0:
                        if not repo_header_printed:
                            if not pull_printed_first:
                                print(B)
                                pull_printed_first = True
                            print(f"  ║ {raw_url}")
                            if pull_git_root:
                                print(f"  ║   ╰─> {pull_git_root}")
                            print(f"  ║ ----------------------------------------------------------------------")
                            repo_header_printed = True
                        print(f"  ║ {subdir_label:<35s} {'--':>17}  {remote_count:>2} remote ({format_size(remote_size):>8})")
            if repo_header_printed:
                print(B)

        sync_codex_pull(
            service, root_folder_id, codex_git_projects, args,
            printed_any=push_printed_any,
        )

    print("Done.")
    return True


def merge_conversations(source_prefix: str, target_prefix: str):
    """Merge source conversation into target, fixing uuid chain and sessionId."""
    import uuid as uuid_mod

    # Find matching JSONL files across all project dirs
    def find_conversation(prefix):
        matches = []
        for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for f in project_dir.glob(f"{prefix}*.jsonl"):
                matches.append(f)
        if not matches:
            print(f"ERROR: No conversation found matching '{prefix}'")
            sys.exit(1)
        if len(matches) > 1:
            print(f"ERROR: Multiple matches for '{prefix}':")
            for m in matches:
                print(f"  {m}")
            sys.exit(1)
        return matches[0]

    source_path = find_conversation(source_prefix)
    target_path = find_conversation(target_prefix)
    target_session = target_path.stem

    print(f"Source: {source_path.name} ({format_size(source_path.stat().st_size)})")
    print(f"Target: {target_path.name} ({format_size(target_path.stat().st_size)})")

    # Backup both
    for p in (source_path, target_path):
        bak = p.with_suffix(".jsonl.bak")
        if not bak.exists():
            import shutil
            shutil.copy2(p, bak)
            print(f"Backup: {bak.name}")

    # Read target — split content from trailing metadata
    target_lines = target_path.read_text().splitlines(keepends=True)
    content_end = len(target_lines)
    for i in range(len(target_lines) - 1, -1, -1):
        d = json.loads(target_lines[i])
        if d.get("type") in ("last-prompt", "custom-title", "system"):
            content_end = i
        else:
            break
    target_content = target_lines[:content_end]
    target_metadata = target_lines[content_end:]

    # Find last uuid in target
    last_target_uuid = None
    for line in reversed(target_content):
        d = json.loads(line)
        if d.get("uuid"):
            last_target_uuid = d["uuid"]
            break

    # Read source — find content range
    source_lines = source_path.read_text().splitlines(keepends=True)
    source_start = 0
    for i, line in enumerate(source_lines):
        d = json.loads(line)
        if d.get("type") in ("user", "assistant"):
            source_start = i
            break
    source_end = len(source_lines)
    for i in range(len(source_lines) - 1, -1, -1):
        d = json.loads(source_lines[i])
        if d.get("type") in ("user", "assistant"):
            source_end = i + 1
            break

    # Remap uuids and sessionIds
    old_to_new = {}
    rewritten = []
    first_content = True
    for line in source_lines[source_start:source_end]:
        d = json.loads(line)
        if d.get("type") not in ("user", "assistant"):
            rewritten.append(line)
            continue

        old_uuid = d.get("uuid")
        if old_uuid:
            new_uuid = str(uuid_mod.uuid4())
            old_to_new[old_uuid] = new_uuid
            d["uuid"] = new_uuid

        old_parent = d.get("parentUuid")
        if old_parent is None and first_content:
            d["parentUuid"] = last_target_uuid
        elif old_parent is not None and str(old_parent) in old_to_new:
            d["parentUuid"] = old_to_new[str(old_parent)]

        if d.get("sessionId"):
            d["sessionId"] = target_session

        first_content = False
        rewritten.append(json.dumps(d, ensure_ascii=False) + "\n")

    # Write merged
    with open(target_path, "w") as f:
        f.writelines(target_content)
        f.writelines(rewritten)
        f.writelines(target_metadata)

    msg_count = sum(1 for l in rewritten
                    if json.loads(l).get("type") in ("user", "assistant"))
    print(f"Merged {msg_count} messages from {source_path.stem[:8]}… into {target_path.stem[:8]}…")
    print(f"Result: {format_size(target_path.stat().st_size)}")


def _kill_pid_tree(pid):
    """Kill a process and all its children."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True)
        for p in result.stdout.strip().split("\n"):
            if p.strip():
                _kill_pid_tree(int(p))
    except (FileNotFoundError, OSError, ValueError):
        pass
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, OSError):
        pass


def _kill_existing_daemon(pid_file: Path, jobs_file: Path = None):
    """Kill any existing daemon process and its children."""
    pids_to_kill = set()

    # From PID file
    if pid_file.exists():
        try:
            pids_to_kill.add(int(pid_file.read_text().strip()))
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)

    # From jobs file _daemon.pid
    if jobs_file and jobs_file.exists():
        try:
            jobs = json.loads(jobs_file.read_text())
            daemon_pid = jobs.get("_daemon", {}).get("pid")
            if daemon_pid:
                pids_to_kill.add(int(daemon_pid))
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    for pid in pids_to_kill:
        _kill_pid_tree(pid)


def _passwordless_sudo_available() -> bool:
    """True only when sudo works without an interactive password prompt."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            timeout=2,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _restore_terminal() -> None:
    """Restore tty after subprocesses (e.g. sudo) that may disable echo."""
    if not sys.stdin.isatty():
        return
    try:
        subprocess.run(
            ["stty", "sane"],
            stdin=sys.stdin,
            capture_output=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass


def _setup_keepalive(keepalive_script: Path, state_dir: Path) -> str:
    """Install a keepalive mechanism. Returns 'cron', 'systemd', or 'watchdog'."""
    keepalive_log = state_dir / "keepalive.log"
    cron_line = f"*/2 * * * * {keepalive_script} >> {keepalive_log} 2>&1"
    use_sudo = _passwordless_sudo_available()

    if use_sudo:
        # Try cron first (requires passwordless sudo — never prompt interactively)
        try:
            result = subprocess.run(["pgrep", "-x", "cron"], capture_output=True)
            if result.returncode != 0:
                subprocess.run(
                    ["sudo", "-n", "service", "cron", "start"],
                    capture_output=True,
                    timeout=5,
                )
                result = subprocess.run(["pgrep", "-x", "cron"], capture_output=True)

            if result.returncode == 0:
                existing = subprocess.run(
                    ["sudo", "-n", "crontab", "-u", os.environ.get("USER", "tiger"), "-l"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if str(keepalive_script) not in existing.stdout:
                    new_crontab = existing.stdout.rstrip("\n")
                    if new_crontab:
                        new_crontab += "\n"
                    new_crontab += cron_line + "\n"
                    proc = subprocess.run(
                        ["sudo", "-n", "crontab", "-u", os.environ.get("USER", "tiger"), "-"],
                        input=new_crontab,
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    if proc.returncode == 0:
                        return "cron"
                else:
                    return "cron"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        # Try systemd user service (only when passwordless sudo is available)
        try:
            result = subprocess.run(
                ["systemctl", "--user", "status"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode in (0, 3):  # 0=running, 3=no units but bus works
                service_name = "claude-history-sync"
                service_dir = Path.home() / ".config" / "systemd" / "user"
                service_dir.mkdir(parents=True, exist_ok=True)
                service_file = service_dir / f"{service_name}.service"
                service_file.write_text(
                    f"[Unit]\n"
                    f"Description=Claude history sync keepalive\n\n"
                    f"[Service]\n"
                    f"Type=oneshot\n"
                    f"ExecStart={keepalive_script}\n"
                    f"Environment=SYNC_STATE_DIR={state_dir}\n\n"
                )
                timer_file = service_dir / f"{service_name}.timer"
                timer_file.write_text(
                    f"[Unit]\n"
                    f"Description=Claude history sync keepalive timer\n\n"
                    f"[Timer]\n"
                    f"OnBootSec=1min\n"
                    f"OnUnitActiveSec=2min\n"
                    f"Persistent=true\n\n"
                    f"[Install]\n"
                    f"WantedBy=timers.target\n"
                )
                subprocess.run(["systemctl", "--user", "daemon-reload"],
                               capture_output=True, timeout=5)
                proc = subprocess.run(
                    ["systemctl", "--user", "enable", "--now", f"{service_name}.timer"],
                    capture_output=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    return "systemd"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # No passwordless sudo: skip cron/systemd (avoid sudo prompts that break the tty)
    return "watchdog"


def _run_daemon_loop(pid_file, jobs_file, service, root_folder_id):
    """Main daemon sync loop."""
    daemon_pid = os.getpid()
    try:
        jobs = json.loads(jobs_file.read_text())
        jobs["_daemon"] = {"pid": daemon_pid}
        jobs_file.write_text(json.dumps(jobs, indent=2))
    except (json.JSONDecodeError, OSError):
        pass

    last_run = {}
    fail_count = {}

    try:
        while True:
            try:
                jobs = json.loads(jobs_file.read_text())
            except (json.JSONDecodeError, OSError):
                jobs = {}

            if not jobs:
                time.sleep(10)
                continue

            now = time.time()
            for job_key, job in list(jobs.items()):
                if job_key.startswith("_"):
                    continue
                job_interval = job.get("interval", 600)
                failures = fail_count.get(job_key, 0)
                effective_interval = min(job_interval * (2 ** failures), job_interval * 4)
                if now - last_run.get(job_key, 0) < effective_interval:
                    continue

                # Lazy-resolve stale jobs: jobs added when the chat ID wasn't
                # yet locally resolvable carry ``repo: null`` and a partial
                # chat_id. Try to fully resolve them now — if we can, rewrite
                # the job under its canonical key so it stops showing up as
                # ``all:<prefix>`` in future runs.
                job_repo = job.get("repo")
                job_chat = job.get("chat_id")
                if (not job_repo or (job_chat and len(job_chat) < 36)) and job_chat:
                    full_chat, chat_repo = resolve_chat_id(job_chat)
                    new_repo = job_repo or chat_repo
                    new_chat = full_chat if len(full_chat) == 36 else job_chat
                    if (new_repo, new_chat) != (job_repo, job_chat):
                        new_key = f"{new_repo or 'all'}:{new_chat or 'all'}"
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[{timestamp}] Resolved job [{job_key}] → [{new_key}]",
                              flush=True)
                        jobs[new_key] = {
                            "repo": new_repo,
                            "chat_id": new_chat,
                            "name": get_chat_name_by_id(new_chat),
                            "interval": job_interval,
                        }
                        if new_key != job_key:
                            jobs.pop(job_key, None)
                        try:
                            jobs_file.write_text(json.dumps(jobs, indent=2))
                        except OSError:
                            pass
                        job = jobs[new_key]
                        job_key = new_key

                job_args = argparse.Namespace(
                    pull_only=False, push_only=False, delete=False,
                    dry_run=False, verbose=False,
                    repo=job.get("repo"), chat_id=job.get("chat_id"),
                    background=None, local=False, board=None,
                )
                try:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(f"\n[{timestamp}] Syncing [{job_key}]...", flush=True)
                    run_sync(job_args, service, root_folder_id)
                    fail_count[job_key] = 0
                except BaseException as e:
                    err_str = str(e)
                    fail_count[job_key] = failures + 1
                    backoff = min(job_interval * (2 ** (failures + 1)), job_interval * 4)
                    # Token expired — try to re-auth for next cycle
                    if "invalid_grant" in err_str or "expired" in err_str.lower():
                        print(f"[TOKEN EXPIRED] Refreshing credentials...", flush=True)
                        try:
                            service = get_drive_service()
                            root_folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)
                            fail_count[job_key] = 0  # reset — next cycle will use fresh creds
                            print(f"[TOKEN REFRESHED] Will retry on next cycle", flush=True)
                        except Exception as auth_e:
                            print(f"[AUTH FAILED] {auth_e} — run: rm token.json && python sync_claude_history.py --dry-run",
                                  flush=True)
                    else:
                        print(f"[ERROR] [{job_key}] {type(e).__name__}: {e} "
                              f"(failure {failures + 1}, next retry in {backoff}s)",
                              flush=True)
                last_run[job_key] = time.time()

            time.sleep(10)
    except KeyboardInterrupt:
        pass
    finally:
        pid_file.unlink(missing_ok=True)


def _run_watchdog(pid_file, jobs_file, log_file, service, root_folder_id):
    """Watchdog: forks a worker, restarts it if it dies. Never returns."""
    import signal

    def _spawn_worker():
        wpid = os.fork()
        if wpid == 0:
            # Worker child
            _run_daemon_loop(pid_file, jobs_file, service, root_folder_id)
            os._exit(0)
        return wpid

    # Write our (watchdog) PID — we're the one that should be killed to stop everything
    pid_file.write_text(str(os.getpid()))
    try:
        jobs = json.loads(jobs_file.read_text())
        jobs["_daemon"] = {"pid": os.getpid()}
        jobs_file.write_text(json.dumps(jobs, indent=2))
    except (json.JSONDecodeError, OSError):
        pass

    worker_pid = _spawn_worker()

    def _cleanup(signum, frame):
        try:
            os.kill(worker_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        pid_file.unlink(missing_ok=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    log_fd = open(log_file, "a")
    while True:
        try:
            _, status = os.waitpid(worker_pid, 0)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            reason = f"exit code {os.WEXITSTATUS(status)}" if os.WIFEXITED(status) else f"signal {os.WTERMSIG(status)}"
            log_fd.write(f"\n[{timestamp}] Worker died ({reason}), restarting in 5s...\n")
            log_fd.flush()
            time.sleep(5)
            worker_pid = _spawn_worker()
        except KeyboardInterrupt:
            _cleanup(None, None)


def main():
    parser = argparse.ArgumentParser(description="Sync Claude Code and Codex history via Google Drive")
    parser.add_argument("--pull", dest="pull_only", action="store_true", help="Only download")
    parser.add_argument("--push", dest="push_only", action="store_true", help="Only upload")
    parser.add_argument("-d", "--delete", action="store_true",
                        help="Delete conversations from Drive (use with --repo and/or --chat)")
    parser.add_argument("--local", action="store_true",
                        help="Delete local conversations instead of remote (use with --delete)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--repo", type=str, default=None,
                        help="Filter to repo(s) (comma-separated, substring match on git remote URL)")
    parser.add_argument("--chat", type=str, default=None, dest="chat_id",
                        help="Filter to conversation(s) (comma-separated, first 8+ chars of session ID)")
    parser.add_argument("--background", type=int, nargs="?", const=600, default=None,
                        metavar="SECONDS",
                        help="Run as background daemon, syncing every N seconds (default: 600)")
    parser.add_argument("--remove-job", action="store_true",
                        help="Remove background job(s) matching --repo/--chat filters")
    parser.add_argument("--merge", nargs=2, metavar=("SOURCE", "TARGET"),
                        help="Merge SOURCE conversation into TARGET (prefix match on session ID)")
    parser.add_argument("--board", nargs="?", const=str(STATE_DIR / "sync_board.html"),
                        default=None, metavar="PATH",
                        help="Generate a local HTML board showing [claude] and [codex] conversations")
    args = parser.parse_args()

    # --background with no value gets None from argparse; treat as 300s default
    # Allow: --background 60, --background 300
    if args.background is not None and args.background <= 0:
        print("ERROR: --background interval must be positive")
        sys.exit(1)

    if args.local and not args.delete:
        print("ERROR: --local requires --delete")
        sys.exit(1)

    if args.delete and not args.repo and not args.chat_id:
        print("ERROR: --delete requires --repo and/or --chat")
        sys.exit(1)

    if args.remove_job and not args.repo and not args.chat_id:
        print("ERROR: --remove-job requires --repo and/or --chat")
        sys.exit(1)

    if args.remove_job:
        jobs_file = STATE_DIR / ".sync_jobs.json"
        pid_file = STATE_DIR / ".sync.pid"
        if not jobs_file.exists():
            print("No background jobs configured.")
            sys.exit(0)
        try:
            jobs = json.loads(jobs_file.read_text())
        except (json.JSONDecodeError, OSError):
            print("No background jobs configured.")
            sys.exit(0)

        repo_filter = args.repo.lower() if args.repo else None
        chat_filter = args.chat_id.lower() if args.chat_id else None
        removed = []
        for key in list(jobs.keys()):
            if key.startswith("_"):
                continue
            job = jobs[key]
            job_repo = (job.get("repo") or "").lower()
            job_chat = (job.get("chat_id") or "").lower()
            match = True
            if repo_filter and repo_filter not in job_repo:
                match = False
            if chat_filter and not job_chat.startswith(chat_filter):
                match = False
            if match:
                removed.append(key)
                del jobs[key]

        if not removed:
            print("No matching background jobs found.")
            sys.exit(0)

        jobs_file.write_text(json.dumps(jobs, indent=2))
        for key in removed:
            print(f"Removed [{key}]")

        remaining = sum(1 for k in jobs if not k.startswith("_"))
        if remaining == 0:
            _kill_existing_daemon(pid_file, jobs_file)
            jobs_file.unlink(missing_ok=True)
            print("No jobs remaining — daemon stopped.")
        else:
            _kill_existing_daemon(pid_file, jobs_file)
            print(f"{remaining} job(s) remaining — restart with: python sync_claude_history.py --background")
        sys.exit(0)

    if not CLAUDE_PROJECTS_DIR.exists():
        CLAUDE_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    if not CODEX_HOME.exists():
        CODEX_HOME.mkdir(parents=True, exist_ok=True)

    if args.board:
        board_path = generate_board(Path(args.board))
        print(f"Board written: {board_path}")
        sys.exit(0)

    # Merge doesn't need Drive access
    if args.merge:
        merge_conversations(args.merge[0], args.merge[1])
        sys.exit(0)

    # Local delete doesn't need Drive access
    if args.delete and args.local:
        run_sync(args, service=None, root_folder_id=None)
        sys.exit(0)

    patch_dns_if_needed()
    service = get_drive_service()
    root_folder_id = get_or_create_folder(service, DRIVE_FOLDER_NAME)

    if args.background is not None:
        interval = args.background
        log_file = STATE_DIR / "sync.log"
        pid_file = STATE_DIR / ".sync.pid"
        jobs_file = STATE_DIR / ".sync_jobs.json"

        # Load existing jobs
        jobs = {}
        if jobs_file.exists():
            try:
                jobs = json.loads(jobs_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        # Split comma-separated repos/chats into individual jobs
        repo_list = [r.strip() for r in args.repo.split(",")] if args.repo else [None]
        chat_list = [c.strip() for c in args.chat_id.split(",")] if args.chat_id else [None]

        # If --background with no repo/chat and jobs already exist, this is a restart
        restart_only = (args.repo is None and args.chat_id is None
                        and any(k for k in jobs if not k.startswith("_")))

        added_jobs = []
        if not restart_only:
            jobs.setdefault("_daemon", {})
            for repo_prefix in repo_list:
                for chat_prefix in chat_list:
                    full_repo = resolve_repo_filter(repo_prefix) if repo_prefix else None
                    full_chat, chat_repo = resolve_chat_id(chat_prefix) if chat_prefix else (None, None)
                    if full_repo is None and chat_repo is not None:
                        full_repo = chat_repo

                    job_key = f"{full_repo or 'all'}:{full_chat or 'all'}"
                    # Remove stale keys that resolve to the same job
                    # (e.g. "all:e520" when we now have the full ID)
                    for old_key in list(jobs.keys()):
                        if old_key.startswith("_") or old_key == job_key:
                            continue
                        old_job = jobs[old_key]
                        old_chat = old_job.get("chat_id", "")
                        if (full_chat and old_chat and
                                full_chat.startswith(old_chat) and old_key != job_key):
                            del jobs[old_key]
                            added_jobs.append(f"Removed stale [{old_key}] (merged into [{job_key}])")
                    old_interval = jobs.get(job_key, {}).get("interval")
                    jobs[job_key] = {
                        "repo": full_repo,
                        "chat_id": full_chat,
                        "name": get_chat_name_by_id(full_chat),
                        "interval": interval,
                    }
                    if old_interval is not None:
                        added_jobs.append(f"Updated [{job_key}]: {old_interval}s -> {interval}s")
                    else:
                        added_jobs.append(f"Added [{job_key}]: every {interval}s")
            jobs_file.write_text(json.dumps(jobs, indent=2))

        # Check if daemon is already running
        daemon_alive = False
        if pid_file.exists():
            try:
                old_pid = int(pid_file.read_text().strip())
                os.kill(old_pid, 0)
                daemon_alive = True
            except (ProcessLookupError, ValueError, OSError):
                pid_file.unlink(missing_ok=True)

        if daemon_alive:
            print(f"Stopping existing daemon (PID {old_pid}) to pick up new code…")

        # Always kill the existing daemon (if any) and orphaned workers,
        # so that rerunning --background picks up new code without the user
        # having to kill the process manually.
        _kill_existing_daemon(pid_file, jobs_file)

        # Sweep any other orphaned sync processes not under the tracked PID
        try:
            result = subprocess.run(
                ["pgrep", "-f", "sync_claude_history.py --background"],
                capture_output=True, text=True,
            )
            self_pid = os.getpid()
            for p in result.stdout.strip().split("\n"):
                if not p.strip():
                    continue
                try:
                    pid_val = int(p)
                except ValueError:
                    continue
                if pid_val == self_pid:
                    continue
                try:
                    os.kill(pid_val, 9)
                except (ProcessLookupError, ValueError, OSError):
                    pass
        except (FileNotFoundError, OSError):
            pass

        # Set up keepalive (cron preferred, watchdog fallback)
        keepalive_script = SCRIPT_DIR / "keepalive.sh"
        keepalive_method = _setup_keepalive(keepalive_script, STATE_DIR)
        _restore_terminal()

        # Fork new daemon
        pid = os.fork()
        if pid > 0:
            # Parent
            job_count = sum(1 for k in jobs if not k.startswith("_"))
            print(f"Background daemon started with {job_count} job(s):")
            for k, v in jobs.items():
                if not k.startswith("_"):
                    print(f"  [{k}] every {v['interval']}s")
            print(f"PID: {pid}")
            print(f"Log: {log_file}")
            print(f"Keepalive: {keepalive_method}")
            print(f"Stop: kill {pid}")
            pid_file.write_text(str(pid))
            sys.stdout.flush()
            sys.stderr.flush()
            # Use os._exit to skip Python cleanup (atexit handlers, thread
            # joins) that can race with the forked child and occasionally
            # produce a non-zero exit code even when everything succeeded.
            os._exit(0)

        # Child: detach and run daemon
        os.setsid()
        log_fd = open(log_file, "a")
        os.dup2(log_fd.fileno(), sys.stdout.fileno())
        os.dup2(log_fd.fileno(), sys.stderr.fileno())

        # If using watchdog, fork again: parent = watchdog, child = worker
        if keepalive_method == "watchdog":
            _run_watchdog(pid_file, jobs_file, log_file, service, root_folder_id)
            # _run_watchdog never returns
        else:
            _run_daemon_loop(pid_file, jobs_file, service, root_folder_id)
    else:
        # Auto-restart background daemon if it died
        jobs_file = STATE_DIR / ".sync_jobs.json"
        pid_file = STATE_DIR / ".sync.pid"
        if jobs_file.exists():
            daemon_dead = False
            if pid_file.exists():
                try:
                    old_pid = int(pid_file.read_text().strip())
                    os.kill(old_pid, 0)
                except (ProcessLookupError, ValueError, OSError):
                    daemon_dead = True
                    pid_file.unlink(missing_ok=True)
            else:
                daemon_dead = True

            if daemon_dead:
                jobs = json.loads(jobs_file.read_text())
                job_count = sum(1 for k in jobs if not k.startswith("_"))
                if job_count:
                    print(f"Restarting background daemon ({job_count} job(s))...")
                    keepalive_script = SCRIPT_DIR / "keepalive.sh"
                    keepalive_method = _setup_keepalive(keepalive_script, STATE_DIR)
                    _restore_terminal()
                    log_file = STATE_DIR / "sync.log"
                    pid = os.fork()
                    if pid == 0:
                        # Child: become daemon with fresh Drive connection
                        os.setsid()
                        log_fd = open(log_file, "a")
                        os.dup2(log_fd.fileno(), sys.stdout.fileno())
                        os.dup2(log_fd.fileno(), sys.stderr.fileno())
                        child_service = get_drive_service()
                        child_root = get_or_create_folder(child_service, DRIVE_FOLDER_NAME)
                        if keepalive_method == "watchdog":
                            _run_watchdog(pid_file, jobs_file, log_file, child_service, child_root)
                        else:
                            _run_daemon_loop(pid_file, jobs_file, child_service, child_root)
                        os._exit(0)
                    # Parent: record PID and continue
                    pid_file.write_text(str(pid))
                    print(f"PID: {pid} (keepalive: {keepalive_method})")

        run_sync(args, service, root_folder_id)


if __name__ == "__main__":
    main()
