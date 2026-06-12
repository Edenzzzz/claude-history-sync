# CLAUDE.md

## Project Overview

CLI tool to sync Claude Code conversation history across machines via Google Drive, organized by git remote URL.

## Development Rules

### Testing

All tests live in `tests/` and use pytest. **Run `pytest tests/ -v` and ensure all tests pass before every push.**

When adding a new flag or functionality, add corresponding test(s) in `tests/test_smoke.py`. Tests check both exit codes and output formatting (no tracebacks, no doubled borders).

When testing resume/trim logic after compaction, verify it aligns with both Claude Code and Codex reconstruction behavior. In particular, check that the retained tail conversation matches the pre-upload/pre-trim conversation tail, and that resuming the reconstructed conversation does not run out of context.

```bash
pytest tests/ -v           # run all tests
pytest tests/ -v -k "dry"  # run subset
```

Tests cover:
- All flag combinations (dry-run, verbose, push, pull, repo/chat filters, delete, background)
- Output formatting (borders, alignment, tree characters)
- Error handling (clean exit, no tracebacks)
- Conversation counts and sizes are accurate

**Important**: Tests must not interfere with running background sync. The background test uses `SYNC_STATE_DIR` (via `tmp_path`) so it writes to a temp dir instead of `.sync.pid`/`.sync_jobs.json`. If you must kill the daemon during development, restore the jobs in sync immediately after with `python sync_claude_history.py --background`.

### Security (CRITICAL)

- **NEVER** commit `credentials.json`, `token.json`, or `service-account.json`
- Verify `.gitignore` blocks these before every push
- If credentials are accidentally committed, scrub with `git filter-branch` and force push immediately

### Commits

- Write concise commit messages documenting the functional change, not implementation details
- For related changes to one feature, squash into one commit instead of committing multiple times for compact history
- Update `CHANGELOG.md` with every commit under the current version section
- Keep changelog entries as single-line bullet points
- Update `README.md` when adding, renaming, or removing flags, features, or changing output format
- When rewriting commits (rebase, amend), preserve original timestamps — do not use `--reset-author` or any flag that modifies the commit date

### Code Style

- Keep the output formatting consistent: `╠═══` borders, `║` left edge, `╰─` tree branches
- Use `format_size()` and `format_time()` for all human-readable output
- The `sync_files()` function takes an `indent` parameter — pass the correct prefix to maintain alignment
