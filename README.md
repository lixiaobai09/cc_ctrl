# cctl

Named tmux workspaces with auto-launched Claude Code, plus a persistent history so you never lose track of past sessions across reboots.

## Why

When you run multiple Claude Code sessions, each typically lives in its own tmux session in a specific working directory. Two things tend to go wrong:

1. You forget which tmux session was working on what.
2. tmux dies (laptop reboot, server crash, last session exited) and you're left with `~/.claude/projects/...` jsonl files but no easy way to remember which `cwd` to `cd` into to resume them.

`cctl` solves both. Each `cctl create <name>` makes a dedicated tmux session named `<name>` running Claude Code in your current directory, and every workspace you ever create is logged to a history file with its cwd — so even after a reboot wipes tmux, you can look up `cctl history` and manually `claude --continue` from the right directory.

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

## Commands

### `cctl create <name> [comment...]`

Creates a new tmux session named `<name>` in the current directory, launches `claude` inside it, and switches you to it. Errors if either a `cctl` workspace or a raw tmux session of that name already exists.

```bash
cctl create auth-refactor "split middleware per legal feedback"
```

Options:

- `--cwd PATH` — override the working directory.
- `--cmd CMD`  — override the command launched in the new session. Use `--cmd ""` to skip running anything.

### `cctl list`

Show currently live workspaces in a table:

```
┏━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ # ┃ name            ┃ comment              ┃ cwd                ┃ session         ┃ created ┃
┡━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ 1 │ auth-refactor   │ split middleware ... │ ~/code/api         │ auth-refactor   │ 12m ago │
│ 2 │ flaky-tests     │ debug shard 4        │ ~/code/api         │ flaky-tests     │ 3m ago  │
└───┴─────────────────┴──────────────────────┴────────────────────┴─────────────────┴─────────┘
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

Each entry shows `name / comment / cwd / status (alive|gone) / last_seen`. Use it to recover after a reboot:

```bash
$ cctl history
# Find the workspace, note its cwd.
$ cd /path/from/history
$ claude --continue
```

### `cctl completion <bash|zsh|fish>`

Print the eval line for shell completion. See above.

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
| `CCTL_HOME`          | `~/.cctl`     | State directory. |
| `CCTL_DEFAULT_CMD`   | `claude`      | Command auto-launched by `cctl create`. |
| `CCTL_NO_SWITCH`     | (unset)       | Skip the tmux switch/attach step after `create`/`go`. Useful for scripting and tests. |

## Caveats

- The `#` ID in `cctl list` is positional — it can shift if a workspace dies between two calls or if you create another in between. Always re-`list` before `go N` if you're unsure. Names are stable.
- `cctl history` records on every `create` and on every `list`. Each entry is keyed by name, so re-creating a workspace with the same name overwrites the prior record's fields (keeping the history compact). If you want full historical timestamps per recreation, file an issue.
- If you only want to look but not nudge `last_seen`, use `cctl peek` instead of `cctl list`.
