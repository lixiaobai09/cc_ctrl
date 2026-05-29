from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import click
from rich.console import Console
from rich.table import Table

CCTL_DIR = Path(os.environ.get("CCTL_HOME", Path.home() / ".cctl"))
STORE_FILE = CCTL_DIR / "workspaces.json"
HISTORY_FILE = CCTL_DIR / "history.json"
DEFAULT_CMD = os.environ.get("CCTL_DEFAULT_CMD", "claude")

console = Console()
err_console = Console(stderr=True)

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@dataclass
class Workspace:
    name: str
    comment: str
    cwd: str
    tmux_session: str
    created_at: str

    @classmethod
    def from_dict(cls, d: dict) -> "Workspace":
        return cls(**{k: d.get(k, "") for k in cls.__dataclass_fields__})


# ---------- storage ----------

@contextmanager
def _locked_store(write: bool = False) -> Iterator[list[Workspace]]:
    CCTL_DIR.mkdir(parents=True, exist_ok=True)
    mode = "r+" if STORE_FILE.exists() else "w+"
    with open(STORE_FILE, mode) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        data = json.loads(raw) if raw else []
        items = [Workspace.from_dict(d) for d in data]

        pruned = False
        if items:
            alive = _live_sessions()
            kept = [w for w in items if w.tmux_session in alive]
            if len(kept) != len(items):
                items = kept
                pruned = True

        yield items
        if write or pruned:
            f.seek(0)
            f.truncate()
            json.dump([asdict(w) for w in items], f, indent=2, ensure_ascii=False)
            f.write("\n")


# ---------- history ----------

def _load_history() -> dict[str, dict]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def _touch_history(workspaces: list[Workspace]) -> None:
    """Upsert history entries for each given workspace. Never deletes."""
    if not workspaces:
        return
    CCTL_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with open(HISTORY_FILE, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        h: dict[str, dict] = json.loads(raw) if raw else {}
        for w in workspaces:
            entry = h.get(w.name, {})
            entry.update({
                "name": w.name,
                "comment": w.comment,
                "cwd": w.cwd,
                "tmux_session": w.tmux_session,
                "created_at": w.created_at,
                "last_seen": now,
            })
            h[w.name] = entry
        f.seek(0)
        f.truncate()
        json.dump(h, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------- lookup ----------

def _find_by_name(items: list[Workspace], name: str) -> Workspace | None:
    for w in items:
        if w.name == name:
            return w
    return None


def _find_by_key(items: list[Workspace], key: str) -> Workspace | None:
    """Look up by 1-based list index (the # column) first, then by name."""
    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(items):
            return items[idx]
    return _find_by_name(items, key)


# ---------- tmux ----------

def _require_tmux() -> None:
    if not shutil.which("tmux"):
        err_console.print("[red]tmux not found in PATH[/red]")
        sys.exit(1)


def _in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _tmux_out(*args: str) -> str:
    return _tmux(*args).stdout.strip()


def _live_sessions() -> set[str]:
    """Names of currently alive tmux sessions. Empty set if tmux server is down."""
    if not shutil.which("tmux"):
        return set()
    ls = _tmux("list-sessions", "-F", "#{session_name}")
    if ls.returncode != 0:
        return set()
    return set(ls.stdout.splitlines())


def _session_exists(name: str) -> bool:
    return name in _live_sessions()


# ---------- CLI ----------

@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option()
def main() -> None:
    """Named tmux sessions with auto-launched Claude Code."""


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("name")
@click.argument("comment", nargs=-1)
@click.option("--cmd", "cmd_override", default=None, help=f"Command to run in the new session (default: {DEFAULT_CMD}). Use '' for none.")
@click.option("--cwd", "cwd_override", default=None, type=click.Path(file_okay=False, dir_okay=True, exists=True), help="Override cwd.")
def create(name: str, comment: tuple[str, ...], cmd_override: str | None, cwd_override: str | None) -> None:
    """Create a tmux session named NAME (auto-launches claude) and switch to it."""
    _require_tmux()

    with _locked_store() as items:
        if _find_by_name(items, name):
            err_console.print(f"[red]workspace '{name}' already exists. pick a different name.[/red]")
            sys.exit(1)

    if _session_exists(name):
        err_console.print(f"[red]tmux session '{name}' already exists. pick a different name.[/red]")
        sys.exit(1)

    cwd = str(Path(cwd_override).resolve()) if cwd_override else os.getcwd()
    cmd = DEFAULT_CMD if cmd_override is None else cmd_override

    new_args = ["new-session", "-d", "-s", name, "-c", cwd]
    if cmd:
        new_args.append(cmd)
    result = _tmux(*new_args)
    if result.returncode != 0:
        err_console.print(f"[red]tmux new-session failed: {result.stderr.strip()}[/red]")
        sys.exit(1)

    record = Workspace(
        name=name,
        comment=" ".join(comment),
        cwd=cwd,
        tmux_session=name,
        created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    )
    with _locked_store(write=True) as items:
        items.append(record)

    _touch_history([record])
    _switch_to(name)


@main.command("list", context_settings=CONTEXT_SETTINGS)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def list_cmd(as_json: bool) -> None:
    """List workspaces."""
    with _locked_store() as items:
        records = list(items)
    _touch_history(records)

    if as_json:
        click.echo(json.dumps([asdict(w) for w in records], indent=2, ensure_ascii=False))
        return

    if not records:
        console.print("[dim]no workspaces[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="bold yellow", justify="right")
    table.add_column("name", style="cyan")
    table.add_column("comment")
    table.add_column("cwd", style="dim")
    table.add_column("session")
    table.add_column("created", style="dim")
    for i, w in enumerate(records, 1):
        table.add_row(
            str(i),
            w.name,
            w.comment or "-",
            _shorten_path(w.cwd),
            f"[green]{w.tmux_session}[/green]",
            _humanize_ts(w.created_at),
        )
    console.print(table)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("name", required=False)
def go(name: str | None) -> None:
    """Switch to the workspace's tmux session."""
    _require_tmux()
    with _locked_store() as items:
        records = list(items)
    if not records:
        err_console.print("[red]no workspaces[/red]")
        sys.exit(1)

    target = _find_by_key(records, name) if name else _pick_interactive(records)
    if target is None:
        err_console.print(f"[red]no workspace matching '{name}'[/red]" if name else "[red]nothing picked[/red]")
        sys.exit(1)

    if not _session_exists(target.tmux_session):
        err_console.print(
            f"[red]tmux session '{target.tmux_session}' is gone.[/red] "
            f"recreate with 'cctl create {target.name}'."
        )
        sys.exit(1)

    _switch_to(target.tmux_session)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("key")
def peek(key: str) -> None:
    """Show one workspace's metadata (KEY can be a name or list index)."""
    with _locked_store() as items:
        w = _find_by_key(items, key)
    if w is None:
        err_console.print(f"[red]no workspace matching '{key}'[/red]")
        sys.exit(1)
    click.echo(json.dumps(asdict(w), indent=2, ensure_ascii=False))


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("-n", "--limit", default=20, type=int, help="Show only the N most recent (0 = all).")
@click.option("--all", "show_all", is_flag=True, help="Show every entry (same as -n 0).")
def history(as_json: bool, limit: int, show_all: bool) -> None:
    """Show workspace history (kept even after tmux dies). Useful for manual recovery."""
    h = _load_history()
    records = sorted(h.values(), key=lambda r: r.get("last_seen", ""), reverse=True)
    if not show_all and limit > 0:
        records = records[:limit]

    if as_json:
        click.echo(json.dumps(records, indent=2, ensure_ascii=False))
        return

    if not records:
        console.print("[dim]no history[/dim]")
        return

    alive = _live_sessions()
    table = Table(show_header=True, header_style="bold")
    table.add_column("name", style="cyan")
    table.add_column("comment")
    table.add_column("cwd", style="dim")
    table.add_column("status")
    table.add_column("last seen", style="dim")
    for r in records:
        is_alive = r.get("tmux_session", "") in alive
        status = "[green]alive[/green]" if is_alive else "[dim]gone[/dim]"
        table.add_row(
            r.get("name", ""),
            r.get("comment") or "-",
            _shorten_path(r.get("cwd", "")),
            status,
            _humanize_ts(r.get("last_seen", "")),
        )
    console.print(table)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Print shell completion. Add to your rc file: eval "$(cctl completion zsh)"."""
    click.echo(f'eval "$(_CCTL_COMPLETE={shell}_source cctl)"')
    click.echo(f'eval "$(_CCT_COMPLETE={shell}_source cct)"')


# ---------- helpers ----------

def _switch_to(session: str) -> None:
    if os.environ.get("CCTL_NO_SWITCH"):
        console.print(f"(no-switch) target [cyan]{session}[/cyan]")
        return
    if _in_tmux():
        _tmux("switch-client", "-t", session)
        console.print(f"switched to [cyan]{session}[/cyan]")
        return
    os.execvp("tmux", ["tmux", "attach", "-t", session])


def _pick_interactive(records: list[Workspace]) -> Workspace | None:
    fzf = shutil.which("fzf")
    if fzf and sys.stdin.isatty():
        lines = [f"{w.name}\t{_shorten_path(w.cwd)}\t{w.comment or '-'}" for w in records]
        result = subprocess.run(
            [fzf, "--with-nth=1..", "--delimiter=\t", "--prompt=cctl go> "],
            input="\n".join(lines),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        picked = result.stdout.split("\t", 1)[0].strip()
        return _find_by_name(records, picked)

    for i, w in enumerate(records, 1):
        click.echo(f"  {i}. {w.name}  [{_shorten_path(w.cwd)}]  {w.comment or ''}")
    choice = click.prompt("pick", type=int, default=1)
    if 1 <= choice <= len(records):
        return records[choice - 1]
    return None


def _shorten_path(p: str) -> str:
    home = str(Path.home())
    return p.replace(home, "~", 1) if p.startswith(home) else p


def _humanize_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = datetime.now(dt.tzinfo) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


if __name__ == "__main__":
    main()
