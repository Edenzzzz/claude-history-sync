# Changelog

## v0.1.7

- Treat GitHub's `ssh.github.com` SSH alias as `github.com` when normalizing repo remotes so both forms sync under the same Drive folder.
- Show remote-only Codex rollouts in the project board so dry-run output names chats that would be pulled instead of only counting them.
- Fix Codex trim equivalence: preserve the final compacted row's full `replacement_history` and exact post-compact rows so `codex resume` opens on the live tail instead of the fork/start prompt.
- **Fix trim severing the resume chain (empty session on resume)**: `trim_at_last_compact` retained rows by file-order suffix, dropping the active leaf's ancestors when the live branch wasn't contiguous in file order (e.g. multiple compactions on divergent branches), which orphaned the compaction summary so `claude --resume` loaded with no context. Now retains rows by `parentUuid` reachability from the active leaf and grafts danglers onto the compaction summary.
- Fix duplicate Codex repo output when both current `_root/_codex__...` and legacy `_codex__root/...` remote rollout folders exist for the same repo/path.
- Require Python 3.10+ with a clear startup error on older interpreters
- Group Codex rollouts by title in sync output: rollouts sharing the same first user message are collapsed into `[codex group]` lines with rollout count and total size; individual rollout file lines are fully suppressed (not shown even in verbose mode). `--chat` now matches Codex titles (e.g. `--chat humanize` selects all rollouts whose title contains "humanize"). Fix duplicate Codex sections in pull phase when multiple remote subfolders map to the same rel_path.
- Skip empty repo sections in sync output: repos where all subfolders have 0 local and 0 remote conversations are no longer displayed (previously showed empty `0 local / 0 remote` rows).
- **Fix `--push` pulling files due to 95% size guard**: the "local shrunk" guard reversed push direction when local files were smaller than remote (e.g. after trim). Now respects `--push` (never pulls) and also skips the guard when a `.pretrim.bak` exists (file was trimmed in a prior sync cycle).
- Fix Codex Drive layout: Codex rollouts now share the normal repo/path folder with Claude conversations and put `_codex__` on each rollout JSONL filename; pulls still read legacy `_codex__<path>` folders.
- Auto-merge conversations when two machines background-sync the same chat: detects when both local and remote have unique entries, merges by timestamp, rebuilds the parentUuid chain, and pushes the merged result
- Add `repair_uuid_chain`: auto-fix broken `parentUuid` links and strip duplicate/orphan uuid entries caused by Claude Code crashes and retries; runs on every sync cycle and after each pull so `claude --resume` can walk the full conversation chain
- Add `--remove-job` flag to remove background sync jobs by `--repo` and/or `--chat` filter; kills daemon when no jobs remain
- **Fix 95% size guard bypassed by stale compact markers**: the guard checked `_file_has_compact_marker` which persists from previous trims, so Claude Code's auto-compaction would shrink the file and bypass the guard, pushing the smaller version and destroying the larger remote. Now only skips the guard for files trimmed in the current sync cycle (`just_trimmed`)
- Add `check_background.sh` to verify the sync daemon process is alive via `ps -aux` + pid from `.sync_jobs.json`, and print configured jobs (repo, chat name, chat ID, interval)
- Store resolved chat `name` (custom title or slug) alongside each `.sync_jobs.json` entry
- **Fix trim breaking Claude Code resume**: preserve original `parentUuid` on compaction entries (dangling ref is how CC detects the trim boundary); reconnect post-trim entries with orphaned parentUuids to the compact entry
- Self-heal background jobs with `repo: null` / partial chat IDs via lazy-resolve in the daemon loop; `resolve_chat_id` dedupes duplicate session-ID matches across project dirs
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
- Self-heal background job entries with `repo: null` / partial chat IDs: the daemon now lazy-resolves the chat ID at each cycle and rewrites the job under its canonical `<repo>:<full-uuid>` key once locally resolvable. `resolve_chat_id` also dedupes duplicate session-ID matches across project dirs (prefers the largest git-rooted copy).
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
