# cctl

Named tmux workspaces with auto-launched Codex (or Claude/qodercli on demand), plus a persistent history so you never lose track of past sessions across reboots.

## Why

When you run multiple coding-agent sessions, each typically lives in its own tmux session in a specific working directory. Two things tend to go wrong:

1. You forget which tmux session was working on what.
2. tmux dies (laptop reboot, server crash, last session exited) and the agent's saved sessions no longer tell you at a glance which `cwd` to return to.

`cctl` solves both. Each `cctl create <name>` makes a dedicated tmux session named `<name>` running Codex in your current directory, and every workspace you ever create is logged to a history file with its cwd — so even after a reboot wipes tmux, you can use `cctl history` and `cctl restore` to get back to the right project.

## Install

Requires Python 3.9+ and tmux.

```bash
pipx install -e /path/to/cc_ctl
```

This installs two console scripts:

- `cctl` — the full command name
- `cct`  — a shorter alias (identical behavior)

## Shell completion

Add this to `~/.zshrc` (or the equivalent for bash/fish):

```bash
eval "$(cctl completion zsh)"
```

It installs completion for both `cctl` and `cct`.

## Codex session tracking setup

Codex assigns its own session UUID, so `cctl` uses a user-level `SessionStart` hook to record it. Install the hook once:

```bash
cctl codex-hook install
codex
```

In Codex, run `/hooks` and trust the cctl hook. After that, every Codex workspace launched by `cctl` records its exact UUID automatically. The hook is inactive in ordinary Codex sessions because it only acts when the `CCTL_WORKSPACE` environment variable is present.

Use `cctl codex-hook status` to check whether the hook is configured, or `cctl codex-hook uninstall` to remove only cctl's handler. Codex owns the trust state, so inspect `/hooks` if the hook is installed but a session ID remains empty.

Capture requires a non-empty `transcript_path` in the hook event. This prevents
ephemeral Codex threads, such as background title generation, from overwriting
the workspace's session ID. The transcript need not exist on disk yet at startup.
Normal startup, resume, clear, and compact events can still update the ID.
Capture decisions are logged to `$CCTL_HOME/codex-capture.jsonl` (default:
`~/.cctl/codex-capture.jsonl`), including the workspace, incoming ID, reason, and
previous ID for accepted events. Prompts and transcript contents are not logged.
`codex-hook status` checks installation only, not the validity of recorded IDs.

## Commands

### `cctl create <name> [comment...]`

Creates a new tmux session named `<name>` in the current directory, then launches `codex` inside the session's shell, and switches you to it. Errors if either a `cctl` workspace or a raw tmux session of that name already exists.

```bash
cctl create auth-refactor "split middleware per legal feedback"
```

The session is started as a normal interactive shell with `codex` sent as a typed command on top — so when you exit Codex (or whatever you ran), you drop back to a live shell prompt instead of the tmux session dying. The session also has `TZ=Asia/Singapore` injected by default (configurable via `CCTL_TZ`).

cctl adds `--no-daemon` to Codex commands on create and restore. This requires
a Codex CLI that supports that option (verified with 0.160.0). Hooks must run in
the workspace's own process environment: a shared daemon can retain another
workspace's `CCTL_WORKSPACE`, causing missing IDs or incorrect updates when two
workspaces use the same directory. Explicit `--remote` commands are rejected.
When manually launching or forking Codex in a cctl shell, also pass
`--no-daemon`, for example `codex fork --no-daemon <session-id>`.
Already-running Codex processes must be exited and resumed with this option;
editing cctl does not change their environment. Fork ID tracking still depends
on Codex emitting a qualifying `SessionStart` event; a fork without that event
will not update the recorded ID automatically.

Claude and qodercli sessions get a fresh UUID via `--session-id`. Codex assigns its UUID internally and the installed `SessionStart` hook writes it back to cctl. All three engines therefore restore the exact recorded session. If the hook is missing or not yet trusted, cctl warns but still starts Codex; that record remains pending until a later hook event captures the ID.

If `--cmd` already contains `--session-id` or `--resume`, or names another binary, Claude/qoder ID injection is skipped — your override wins.

Options:

- `-c`, `--claude` — launch `claude` instead of the default `codex`.
- `-q`, `--qoder` — launch `qodercli` instead of the default `codex`.
- `--cwd PATH` — override the working directory.
- `--cmd CMD`  — override the command launched in the new session. Use `--cmd ""` to skip running anything.

### `cctl list`

Show currently live workspaces in a table:

```
┏━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ # ┃ name            ┃ engine   ┃ comment              ┃ cwd                ┃ created ┃
┡━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ 1 │ auth-refactor   │ codex    │ split middleware ... │ ~/code/api         │ 12m ago │
│ 2 │ ui-redesign     │ qodercli │ try qoder run        │ ~/code/ui          │ 3m ago  │
└───┴─────────────────┴──────────┴──────────────────────┴────────────────────┴─────────┘
```

The `#` column is the short ID for `cctl go`. Output `--json` for scripting.

Dead workspaces (tmux session is gone) are pruned automatically on every read.

### `cctl go <name|N>`

Switch to a workspace. Accepts either the workspace name or its row number from `cctl list`:

```bash
cctl go 1               # by ID
cctl go auth-refactor   # by name
cctl go                 # no arg → fzf picker if available, else numbered prompt
```

Inside tmux, this uses `switch-client`. From outside tmux, it `attach`es.

### `cctl peek <name|N>`

Print the workspace metadata as JSON without switching to it.

### `cctl history`

Shows every workspace you've ever created via `cctl`, including dead ones. This file is **never auto-pruned** — it survives tmux restarts and reboots.

```bash
cctl history          # most recent 20
cctl history -n 50    # most recent 50
cctl history --all    # everything
cctl history --json   # for scripts
```

Each entry shows `name / engine / comment / cwd / session_id / status (alive|gone) / last_seen`. Use it (or `cctl restore` below) to recover after a reboot.

### `cctl restore <name>`

Recreates a workspace from a history entry in the recorded cwd. Claude and qodercli use `<engine> --resume <session_id>`; Codex uses `codex resume <session_id>`. This is the post-reboot equivalent of `cctl create`.

```bash
cctl restore auth-refactor                   # bring back exactly as before
cctl restore auth-refactor --as auth-redo    # restore under a new name (e.g. live record still exists)
cctl restore auth-refactor --cwd ~/code/api  # original cwd moved; resume from a different path
```

Errors if a live workspace / tmux session with the target name already exists.

If the history entry has no `session_id` (for example, a Codex workspace created before its hook was trusted), `restore` prompts before resuming the most recent engine session in the cwd. For Codex this runs `codex resume --last`; once resumed, the hook backfills the actual UUID automatically. Confirm `y` to proceed or `n` to abort.

If `claude --resume <uuid>` fails because the jsonl session file is gone (deleted, machine wiped), the tmux session is still created so you can manually `claude --continue` from there.

### `cctl completion <bash|zsh|fish>`

Print the eval line for shell completion. See above.

### `cctl codex-hook <install|status|uninstall>`

Manage the user-level Codex `SessionStart` hook used for exact session-ID tracking. Installation merges into `$CODEX_HOME/hooks.json` without replacing unrelated hooks. See [Codex session tracking setup](#codex-session-tracking-setup).

## How identity works

- Workspace name **is** the tmux session name. `cctl create foo` runs `tmux new-session -s foo`.
- Two-way collision check at create time: errors if either a `cctl` record or an unrelated tmux session named `foo` already exists.
- When the tmux session named `foo` dies (you exit it, kill-server, reboot), the live workspace record for `foo` is removed from `cctl list` on the next read — but `cctl history` keeps its cwd so you can recover.

## Files

| Path | Purpose |
|---|---|
| `~/.cctl/workspaces.json` | Live workspaces. Auto-pruned to match tmux reality. |
| `~/.cctl/history.json`    | Permanent log keyed by name. Never auto-pruned. |

Override the directory with `CCTL_HOME=/somewhere`.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `CCTL_HOME`          | `~/.cctl`         | State directory. |
| `CCTL_DEFAULT_CMD`   | `codex`           | Command auto-launched by `cctl create` (also used to restore Codex entries). |
| `CCTL_CLAUDE_CMD`    | `claude`          | Command used by `cctl create -c` and to restore Claude entries. |
| `CCTL_QODER_CMD`     | `qodercli`        | Command auto-launched by `cctl create -q` (also used by `cctl restore` for `engine=qodercli` entries). |
| `CCTL_TZ`            | `Asia/Singapore`  | `TZ` env var injected into the new tmux session. Set to empty to skip. |
| `CCTL_NO_SWITCH`     | (unset)           | Skip the tmux switch/attach step after `create`/`go`. Useful for scripting and tests. |

The Codex hook location follows `CODEX_HOME` (default: `~/.codex`).

## Caveats

- The `#` ID in `cctl list` is positional — it can shift if a workspace dies between two calls or if you create another in between. Always re-`list` before `go N` if you're unsure. Names are stable.
- `cctl history` records on every `create` and on every `list`. Each entry is keyed by name, so re-creating a workspace with the same name overwrites the prior record's fields (keeping the history compact). If you want full historical timestamps per recreation, file an issue.
- If you only want to look but not nudge `last_seen`, use `cctl peek` instead of `cctl list`.
- Running Codex `/new` inside a cctl workspace updates that workspace to the new session UUID, so a later `restore` follows the session most recently active in that tmux workspace.

## Mobile web terminal (optional)

The web server is opt-in. It supports existing workspace attach, exact history
restore, window/pane selection, and manual mobile sizing. It does not create new
workspaces from the browser. Run it as the same OS user as your tmux sessions.

Python 3.9 is supported for both the CLI and web server. Its web dependencies
use compatible release ranges (Starlette <0.50, Uvicorn <0.40, websockets <16,
AnyIO <4.13); newer Python environments can use newer releases. Python 3.9.6
was verified with Starlette 0.49.3, Uvicorn 0.39.0, websockets 15.0.1 and
AnyIO 4.12.1, including the HTTPS browser smoke test.

Install with Python 3.9+:

```bash
python3 -m pip install -e '.[web]'
cct server init
```

If you already installed an older cct, reinstall it using Python 3.9+ and update
its Codex hook with the **same installation**:

```bash
cct codex-hook install
```

Trust the updated handler in Codex `/hooks`. Mixing an old hook executable with
new workspace records can discard the new identity fields. The updated hook uses
the installing Python interpreter rather than looking up another cct on PATH.

`init` prompts for an administrator username and a password of at least 12
characters. Passwords use Argon2id; each browser gets a separate revocable login.
Account setup does not start a network listener. CLI commands keep working without
web dependencies.

### HTTPS on a home/studio network

Use a stable LAN hostname or reserved IP. Obtain a certificate whose SAN includes
that hostname/IP and install the issuing CA certificate as trusted on the phone.
An existing trusted certificate also works. For a private LAN CA, OpenSSL example:

```bash
mkdir -p ~/.cctl/tls
chmod 700 ~/.cctl/tls
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
  -keyout ~/.cctl/tls/ca.key -out ~/.cctl/tls/ca.crt \
  -subj '/CN=cct LAN CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign'
openssl req -newkey rsa:3072 -nodes \
  -keyout ~/.cctl/tls/server.key -out ~/.cctl/tls/server.csr \
  -subj '/CN=cct.home.arpa'
cat > ~/.cctl/tls/server.ext <<'EOF'
subjectAltName=DNS:cct.home.arpa,IP:192.168.1.50
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -in ~/.cctl/tls/server.csr \
  -CA ~/.cctl/tls/ca.crt -CAkey ~/.cctl/tls/ca.key -CAcreateserial \
  -out ~/.cctl/tls/server.crt -days 365 -extfile ~/.cctl/tls/server.ext
chmod 600 ~/.cctl/tls/*.key
```

Replace the example hostname/IP with your machine's actual address, and configure
local DNS if using the hostname. Keep the CA private key on the computer; transfer
only `ca.crt` to the phone. On iOS install the CA profile and enable full trust in
Settings → General → About → Certificate Trust Settings. On Android install the CA
as a user CA in the device's certificate/security settings; verify it is trusted
by your chosen browser. OpenSSL 3 supports the commands above; on macOS you can
use Homebrew's `openssl` if the system command lacks `-addext`.

Start the foreground service:

```bash
cct serve --host 0.0.0.0 --port 8443 \
  --cert-file ~/.cctl/tls/server.crt \
  --key-file ~/.cctl/tls/server.key \
  --origin https://192.168.1.50:8443
```

Open exactly that HTTPS address on the phone. `--origin` must match the browser's
address including the port; another hostname or HTTP access is rejected. Without
`--host` the service only listens on `127.0.0.1`. There is no anonymous/HTTP mode.
The phone and host must have network connectivity, and the host must stay awake.
Stop with Ctrl-C: attached web clients disconnect while agents remain in tmux.

### Terminal behavior

- The terminal view uses a compact header and toolbar; login management and
  connection notes are in the menu. All bottom shortcuts share one row, without
  a separate draft input. Scrollbars have reserved space outside terminal text.
- Default terminal font size is 8px. Use Menu → terminal font size to decrease,
  increase or reset it (8–20px). The browser remembers the choice. When mobile
  adaptation is active, changing font size recalculates the shared window geometry.
- Swipe vertically over the terminal to scroll the active pane. Before adaptation,
  a narrow scroll control is also available on the right; after adaptation it is
  hidden so the terminal can use the full width. Arrow buttons and desktop wheel also work.
  This requires web control when scrolling the remote pane: mouse-aware apps such
  as Codex receive wheel events; shells use tmux copy-mode history. Scrolling down
  to the end exits tmux copy-mode. The rail controls scrolling, not the app's exact
  transcript position. No global tmux mouse settings are changed.
- First open selects the lowest-index window, whether numbering starts at 0 or 1.
  Reconnecting follows the current window without resetting focus.
- The entire selected window, including splits, is rendered. Input goes to its
  active pane. Window/pane selection is shared with local tmux clients.
- One browser controls each workspace run; others can watch or explicitly take
  control. Local tmux clients can still type/switch at any time.
- Tap the terminal prompt to type using the native terminal input. Bottom shortcuts
  (Esc, Tab, arrows, Ctrl-C, Paste, Enter) occupy one row. Paste reads the browser
  clipboard on click and transfers text without Enter; if clipboard access is
  denied, long-press the terminal input to use the browser's paste action. Enter
  sends a terminal carriage return. Input is never resent after a disconnect.
- Default sizing preserves the existing terminal view and allows scrolling. Click
  **适配手机** to size the current window to the phone; this also changes that
  window on the desktop. Rotation/keyboard changes update the size while enabled.
- Release, takeover, window change, disconnect or shutdown restores the previous
  size policy. Manual sizing restores original dimensions; inherited options remain
  inherited. Crash recovery runs on next server startup. Independent user changes
  are preserved. Small split layouts may require scrolling and pane proportions
  may change when resized; no automatic pane zoom is performed.
- History restore requires an exact saved UUID and an existing directory. Missing
  IDs/paths or name collisions must be handled with local `cct restore`. Restoring
  does not recreate additional windows or split layouts.

### Login management

```bash
cct server sessions list
cct server sessions revoke LOGIN_ID
cct server sessions revoke --all
cct server reset-password
```

Ordinary logins use a browser-session cookie with a 12-hour server lifetime;
“remember login” lasts up to 30 days. Closing a browser is not a reliable logout;
use the logout button. Password reset revokes all logins. Revocation closes active
terminal connections within five seconds. All logged-in browsers have administrator
access to the host user's terminals. Credentials, sessions and size recovery are in
`$CCTL_HOME/server/auth.sqlite3`, protected by user-only permissions.

Existing JSON records gain stable workspace/run IDs on first server startup, with
`*.pre-web.bak` backups. Web queries do not update history timestamps or remove
records on tmux errors. New agent launches also send workspace/run IDs to hooks;
existing processes retain legacy tracking until relaunched. The earlier Codex fork
hook limitation still applies.

### Development and verification

```bash
python3 -m pip install -e '.[web,test]'
cd web
npm ci
npm run build
cd ..
python3 -m unittest discover -s tests -v
python3 -m build
```

The frontend build writes committed assets to `cctl/static`. Build it before
packaging after frontend changes. Published wheels include those files and do not
need Node.js at runtime. Tests use temporary state and an isolated tmux socket.
The implementation contract is in [docs/remote-control-plan.md](docs/remote-control-plan.md).

Optional browser smoke test (Chrome and OpenSSL required):

```bash
PYTHONPATH=. python3 tests/browser_smoke.py
```

It launches a temporary HTTPS server and isolated headless browser, uses a temporary
certificate, and validates login, mobile rendering, sizing, window selection and
PTY input. It prints a screenshot path. On Linux set `CCT_CHROME` to the Chrome or
Chromium binary. TLS verification bypass is confined to this synthetic test browser;
normal deployment requires a certificate trusted by the phone.
