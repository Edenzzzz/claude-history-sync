# Changelog

## v0.1.7

- Add DNS-over-HTTPS fallback when googleapis.com is unreachable via local DNS (patches socket + httplib2)
- Speed up sync by batching Google Drive API calls (`batch_list_remote_files`, `batch_list_drive_folders`)
- Store raw git URL in folder `description` to skip metadata file downloads on subsequent runs
- Eliminate duplicate `list_remote_files` calls by passing pre-fetched data to `sync_files`
- Sync `memory/` folder (MEMORY.md + memory files) alongside conversation history
- Add `-d` short form for `--delete` and `--local` flag to delete local conversations
- `--delete` now requires `--repo` and/or `--chat` (either or both)
- Add `--merge SOURCE TARGET` to merge conversations (fixes uuid chain and sessionId)
- Auto-clean empty local conversations (no assistant reply) on every sync run
- Fix `sync_memory` crash when `list_drive_folders` returns string IDs
- Add pytest smoke tests for all flag combinations (`tests/test_smoke.py`)
- Skip gitignored dirs from sync, apply `--chat` filter in pull phase and skip memory
- Fix zombie daemon processes by killing PID tree before starting new daemon
- Auto-create Claude project dir instead of crashing when it doesn't exist
- Make memory sync chat-specific on Drive using `originSessionId` frontmatter (Drive structure: `_memory/<chat_id>/*.md`), auto-migrate legacy flat memory files to largest chat
- Auto-rewrite `cwd` fields on pull to the local project dir's path, so `claude --resume` no longer fails with *"This conversation is from a different directory"* on machines where the repo lives at a different path (mtime preserved to avoid sync churn)
- Auto-trim conversations at their last `/compact` point on every push/pull — keeps only the compact summary message and everything after it, so oversized chats become small enough for `claude --resume` to load. Original mtime is preserved so sync direction is unchanged; a `.pretrim.bak` is written the first time a file is trimmed.
- `--background` now always kills and restarts the existing daemon so code changes get picked up without manually stopping it first

## v0.1.6

- Add `--background` flag for auto-sync daemon (default: every 10 min), writes PID to `.sync.pid`

## v0.1.5

- Fix scan_local_git_repos hanging by limiting os.walk depth and stopping at .git boundaries
- Fix missing bottom border and doubled separator between push/pull sections in output
- Add `--repo` filter (comma-separated, substring match on git remote URL)
- Add `--chat_id` filter (comma-separated, prefix match on session ID)
- Add `--delete` to remove conversations from Drive (repo-wide delete requires confirmation)
- Skip empty conversations (no assistant response) during sync
- Cross-machine project resolution: scan sibling repos and match by git remote, cache results in `.repo_cache.json`

## v0.1.1

- Sync conversation titles (custom-title / slug) across machines via `_titles.json`; on pull, inject title into downloaded JSONL so conversations show named in `/resume`
- Replace custom-title in-place instead of appending to prevent duplicate entries that cause title revert

## v0.1.0

- Initial release: bidirectional sync of Claude Code conversation history via Google Drive
- Organize Drive folders by normalized git remote URL with subfolders by relative path within repo
- Resolve ambiguous Claude project dir names (hyphens vs path separators vs underscores) by checking filesystem
- Support OAuth (with headless fallback) and service account authentication
- `--push`, `--pull`, `--dry-run`, `-v` flags
- Verbose mode lists each conversation with ID, title, size, date
- Tabular output with `╠═══` / `║` / `╰─` box drawing
