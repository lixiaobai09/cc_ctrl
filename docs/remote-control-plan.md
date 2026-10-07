# Remote control implementation contract

Single-admin, single-host, optional foreground HTTPS server for macOS/Linux.
Existing CLI remains usable without web dependencies. Browser supports existing
workspace attach and exact history restore, never workspace creation or ambiguous
`resume --last`. Restoration does not reconstruct additional windows or splits.

Default window is the lowest index (not necessarily index 1). Desktop and mobile
share window/pane selection. Web clients share one explicit writer lease per run;
local tmux clients remain independent writers. Web selection uses window/pane IDs.

Mobile resizing is opt-in, scoped to the writer and selected window. Save original
size and window-size option inheritance, journal before mutation, restore on window
switch, lease release/takeover, disconnect, shutdown, or next startup after crash.
Never overwrite a window whose identity or settings changed independently. Multiple
panes remain visible; no automatic zoom or split reconstruction.

Account setup/reset is local interactive CLI. Argon2id password hash, opaque cookie
tokens stored hashed in SQLite, CSRF for mutations, exact Host/Origin checks, login
rate limits. Normal session 12h; remembered session 30d. Password reset revokes all.
WebSocket rechecks revocation within 5 seconds. TLS cert/key mandatory, no auth bypass.

Read-only web queries never touch last_seen or prune on tmux query failure. JSON
storage uses independent locks and atomic writes; old identities migrate with backup.
New hooks validate workspace/run identity; retain Codex --no-daemon behavior.

Frontend: TypeScript/Vite/xterm.js bundled locally. Backend: Starlette/Uvicorn.
PTY attach with ignore-size; output binary frames and JSON control messages.
Disconnect never retries input or kills agent/session. Test against isolated tmux
socket and temporary state, not live user workspaces. Ship built static files.

Compatibility: Python 3.9+ for CLI and web. Python-version dependency markers
select 3.9-compatible server releases; newer interpreters keep their newer ranges.
Browser smoke test uses the WebSocket context manager shared by these versions.
