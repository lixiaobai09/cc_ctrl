# cctl

Named tmux workspaces with auto-launched Codex (or Claude/qodercli on demand), plus a persistent history so you never lose track of past sessions across reboots.

## Why

When you run multiple coding-agent sessions, each typically lives in its own tmux session in a specific working directory. Two things tend to go wrong:

1. You forget which tmux session was working on what.
2. tmux dies (laptop reboot, server crash, last session exited) and the agent's saved sessions no longer tell you at a glance which `cwd` to return to.

`cctl` solves both. Each `cctl create <name>` makes a dedicated tmux session named `<name>` running Codex in your current directory, and every workspace you ever create is logged to a history file with its cwd — so even after a reboot wipes tmux, you can use `cctl history` and `cctl restore` to get back to the right project.

## Install

Requires Python 3.10+ and tmux.

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
