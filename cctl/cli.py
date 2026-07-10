from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
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
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_HOOKS_FILE = CODEX_HOME / "hooks.json"
CODEX_HOOKS_LOCK_FILE = CODEX_HOME / "hooks.json.cctl.lock"
DEFAULT_CMD = os.environ.get("CCTL_DEFAULT_CMD", "codex")
CLAUDE_CMD = os.environ.get("CCTL_CLAUDE_CMD", "claude")
QODER_CMD = os.environ.get("CCTL_QODER_CMD", "qodercli")
DEFAULT_TZ = os.environ.get("CCTL_TZ", "Asia/Singapore")

KNOWN_ENGINES = {"codex", "claude", "qodercli"}
SESSION_ID_ENGINES = {"claude", "qodercli"}
ENGINE_STYLES = {"codex": "green", "claude": "cyan", "qodercli": "magenta"}
CODEX_HOOK_STATUS = "Recording Codex session in cctl"

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
    session_id: str = ""
    engine: str = ""

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
            entry.update({**asdict(w), "last_seen": now})
            h[w.name] = entry
        f.seek(0)
        f.truncate()
        json.dump(h, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------- Codex hook ----------

@contextmanager
def _locked_codex_hooks(write: bool = False) -> Iterator[dict]:
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    with open(CODEX_HOOKS_LOCK_FILE, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if CODEX_HOOKS_FILE.exists():
            try:
                data = json.loads(CODEX_HOOKS_FILE.read_text() or "{}")
            except json.JSONDecodeError as exc:
                raise click.ClickException(f"cannot parse {CODEX_HOOKS_FILE}: {exc}") from exc
        else:
            data = {}
        if not isinstance(data, dict):
            raise click.ClickException(f"{CODEX_HOOKS_FILE} must contain a JSON object")

        yield data
        if write:
            _write_json_atomic(CODEX_HOOKS_FILE, data)


def _write_json_atomic(path: Path, data: dict) -> None:
    # Keep dotfile-manager symlinks intact by atomically replacing their target.
    write_path = path.resolve() if path.is_symlink() else path
    write_path.parent.mkdir(parents=True, exist_ok=True)
    old_mode = write_path.stat().st_mode & 0o777 if write_path.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{write_path.name}.", dir=write_path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if old_mode is not None:
            os.chmod(temp_name, old_mode)
        os.replace(temp_name, write_path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _codex_capture_handler() -> dict:
    cctl_binary = shutil.which("cctl")
    if not cctl_binary:
        raise click.ClickException("cctl is not in PATH; install the package before installing its Codex hook")
    return {
        "type": "command",
        "command": f"{shlex.quote(cctl_binary)} codex-hook capture",
        "timeout": 5,
        "statusMessage": CODEX_HOOK_STATUS,
    }


def _is_cctl_capture_handler(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    command = value.get("command")
    return (
        value.get("type") == "command"
        and value.get("statusMessage") == CODEX_HOOK_STATUS
        and isinstance(command, str)
        and command.endswith(" codex-hook capture")
    )


def _codex_hook_installed_in(data: dict) -> bool:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get("SessionStart")
    if not isinstance(groups, list):
        return False
    return any(
        _is_cctl_capture_handler(handler)
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for handler in group["hooks"]
    )


def _remove_cctl_capture_handlers(data: dict) -> bool:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get("SessionStart")
    if not isinstance(groups, list):
        return False

    removed = False
    kept_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        handlers = group["hooks"]
        kept_handlers = [handler for handler in handlers if not _is_cctl_capture_handler(handler)]
        removed = removed or len(kept_handlers) != len(handlers)
        if kept_handlers:
            updated = dict(group)
            updated["hooks"] = kept_handlers
            kept_groups.append(updated)
    if removed:
        hooks["SessionStart"] = kept_groups
    return removed


def _codex_hook_installed() -> bool:
    if not CODEX_HOOKS_FILE.exists():
        return False
    try:
        data = json.loads(CODEX_HOOKS_FILE.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and _codex_hook_installed_in(data)


def _warn_if_codex_hook_missing() -> None:
    if not _codex_hook_installed():
        err_console.print(
            "[yellow]warning: cctl's Codex SessionStart hook is not installed; "
            "this session may only be restorable with `codex resume --last`. "
            "Run `cctl codex-hook install`, then trust it in Codex `/hooks`.[/yellow]"
        )


def _capture_codex_session(payload: object, workspace_name: str) -> bool:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return False
    raw_session_id = payload.get("session_id")
    hook_cwd = payload.get("cwd")
    if not isinstance(raw_session_id, str) or not isinstance(hook_cwd, str):
        return False
    try:
        session_id = str(uuid.UUID(raw_session_id))
    except ValueError:
        return False

    updated: Workspace | None = None
    with _locked_store(write=True) as items:
        workspace = _find_by_name(items, workspace_name)
        if workspace is None or workspace.engine != "codex":
            return False
        if os.path.realpath(workspace.cwd) != os.path.realpath(hook_cwd):
            return False
        if workspace.session_id == session_id:
            return True
        workspace.session_id = session_id
        updated = workspace

    if updated is not None:
        _touch_history([updated])
    return True


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
    """Named tmux sessions with auto-launched coding agents."""


@main.group("codex-hook", context_settings=CONTEXT_SETTINGS)
def codex_hook() -> None:
    """Install and manage Codex session-ID capture."""


@codex_hook.command("install", context_settings=CONTEXT_SETTINGS)
def codex_hook_install() -> None:
    """Install the user-level Codex SessionStart hook."""
    handler = _codex_capture_handler()
    with _locked_codex_hooks(write=True) as data:
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise click.ClickException(f"the 'hooks' value in {CODEX_HOOKS_FILE} must be an object")
        if not isinstance(hooks.setdefault("SessionStart", []), list):
            raise click.ClickException(f"hooks.SessionStart in {CODEX_HOOKS_FILE} must be an array")
        _remove_cctl_capture_handlers(data)
        hooks["SessionStart"].append({"hooks": [handler]})

    console.print(f"[green]installed cctl Codex hook in {CODEX_HOOKS_FILE}[/green]")
    console.print("Open Codex, run [bold]/hooks[/bold], and trust the cctl hook once before using it.")


@codex_hook.command("status", context_settings=CONTEXT_SETTINGS)
def codex_hook_status() -> None:
    """Check whether the cctl hook is configured."""
    if _codex_hook_installed():
        console.print(f"[green]installed[/green] in {CODEX_HOOKS_FILE}")
        console.print("Trust state is managed by Codex; use [bold]/hooks[/bold] to inspect it.")
        return
    console.print(f"[yellow]not installed[/yellow] in {CODEX_HOOKS_FILE}")
    raise click.exceptions.Exit(1)


@codex_hook.command("uninstall", context_settings=CONTEXT_SETTINGS)
def codex_hook_uninstall() -> None:
    """Remove only cctl's Codex hook."""
    with _locked_codex_hooks(write=True) as data:
        removed = _remove_cctl_capture_handlers(data)
    if removed:
        console.print(f"[green]removed cctl Codex hook from {CODEX_HOOKS_FILE}[/green]")
    else:
        console.print(f"[dim]cctl Codex hook was not installed in {CODEX_HOOKS_FILE}[/dim]")


@codex_hook.command("capture", hidden=True)
def codex_hook_capture() -> None:
    """Receive a Codex SessionStart event and update its cctl workspace."""
    workspace_name = os.environ.get("CCTL_WORKSPACE", "")
    if not workspace_name:
        return
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    _capture_codex_session(payload, workspace_name)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("name")
@click.argument("comment", nargs=-1)
@click.option("-c", "--claude", "use_claude", is_flag=True, help=f"Launch `{CLAUDE_CMD}` instead of `{DEFAULT_CMD}`.")
@click.option("-q", "--qoder", "use_qoder", is_flag=True, help=f"Launch `{QODER_CMD}` instead of `{DEFAULT_CMD}`.")
@click.option("--cmd", "cmd_override", default=None, help=f"Full command override (default: {DEFAULT_CMD}; {CLAUDE_CMD} with -c; {QODER_CMD} with -q). Use '' for none.")
@click.option("--cwd", "cwd_override", default=None, type=click.Path(file_okay=False, dir_okay=True, exists=True), help="Override cwd.")
def create(name: str, comment: tuple[str, ...], use_claude: bool, use_qoder: bool, cmd_override: str | None, cwd_override: str | None) -> None:
    """Create a tmux session named NAME (auto-launches codex) and switch to it."""
    _require_tmux()

    if use_claude and use_qoder:
        raise click.UsageError("-c/--claude and -q/--qoder are mutually exclusive")

    with _locked_store() as items:
        if _find_by_name(items, name):
            err_console.print(f"[red]workspace '{name}' already exists. pick a different name.[/red]")
            sys.exit(1)

    if _session_exists(name):
        err_console.print(f"[red]tmux session '{name}' already exists. pick a different name.[/red]")
        sys.exit(1)

    cwd = str(Path(cwd_override).resolve()) if cwd_override else os.getcwd()
    if cmd_override is not None:
        cmd = cmd_override
    elif use_claude:
        cmd = CLAUDE_CMD
    elif use_qoder:
        cmd = QODER_CMD
    else:
        cmd = DEFAULT_CMD
    engine = _detect_engine(cmd)
    session_id = str(uuid.uuid4()) if engine in SESSION_ID_ENGINES else ""
    if engine == "codex":
        _warn_if_codex_hook_missing()

    # Start a bare shell, not the command itself. That way `/quit` (or whatever
    # exits cmd) leaves the user at a live shell prompt instead of tearing the
    # tmux session down with cmd's exit.
    new_args = ["new-session", "-d", "-s", name, "-c", cwd]
    if DEFAULT_TZ:
        new_args.extend(["-e", f"TZ={DEFAULT_TZ}"])
    if engine == "codex":
        new_args.extend(["-e", f"CCTL_WORKSPACE={name}", "-e", f"CCTL_HOME={CCTL_DIR.resolve()}"])
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
        session_id=session_id,
        engine=engine,
    )
    with _locked_store(write=True) as items:
        items.append(record)
    _touch_history([record])

    # Persist the record before launching Codex: SessionStart can fire as soon
    # as the command starts and needs a workspace to update.
    if cmd:
        launch = _inject_session_id(cmd, session_id)
        _tmux("send-keys", "-t", name, launch, "Enter")

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
    table.add_column("engine")
    table.add_column("comment")
    table.add_column("cwd", style="dim")
    table.add_column("created", style="dim")
    for i, w in enumerate(records, 1):
        table.add_row(
            str(i),
            w.name,
            _engine_cell(w.engine),
            w.comment or "-",
            _shorten_path(w.cwd),
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
    table.add_column("engine")
    table.add_column("comment")
    table.add_column("cwd", style="dim")
    table.add_column("session id", style="dim")
    table.add_column("status")
    table.add_column("last seen", style="dim")
    for r in records:
        is_alive = r.get("tmux_session", "") in alive
        status = "[green]alive[/green]" if is_alive else "[dim]gone[/dim]"
        table.add_row(
            r.get("name", ""),
            _engine_cell(r.get("engine") or ""),
            r.get("comment") or "-",
            _shorten_path(r.get("cwd", "")),
            r.get("session_id") or "-",
            status,
            _humanize_ts(r.get("last_seen", "")),
        )
    console.print(table)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.argument("name")
@click.option("--as", "new_name", default=None, help="Restore under a different workspace name (default: same as history entry).")
@click.option("--cwd", "cwd_override", default=None, type=click.Path(file_okay=False, dir_okay=True, exists=True), help="Override cwd (default: recorded cwd).")
def restore(name: str, new_name: str | None, cwd_override: str | None) -> None:
    """Restore a workspace from history via its recorded coding agent."""
    _require_tmux()
    h = _load_history()
    entry = h.get(name)
    if entry is None:
        err_console.print(f"[red]no history entry named '{name}'[/red]")
        sys.exit(1)
    session_id = (entry.get("session_id") or "").strip()
    engine = (entry.get("engine") or "claude").strip()
    binary = _engine_binary(engine)
    if engine == "codex":
        _warn_if_codex_hook_missing()
    if not session_id:
        reason = "Codex assigns its own session IDs" if engine == "codex" else "the entry likely predates session tracking"
        fallback_cmd = _resume_command(binary, engine, "")
        console.print(f"[yellow]history entry '{name}' has no session_id ({reason}).[/yellow]")
        if not click.confirm(
            f"fall back to `{fallback_cmd}` (resume the most recent {engine} session in the cwd)?",
            default=False,
        ):
            err_console.print("[red]aborted.[/red]")
            sys.exit(1)

    target = new_name or name

    with _locked_store() as items:
        if _find_by_name(items, target):
            err_console.print(f"[red]workspace '{target}' already exists. pass --as <other-name>.[/red]")
            sys.exit(1)
    if _session_exists(target):
        err_console.print(f"[red]tmux session '{target}' already exists. pass --as <other-name>.[/red]")
        sys.exit(1)

    recorded_cwd = entry.get("cwd") or ""
    cwd = str(Path(cwd_override).resolve()) if cwd_override else (recorded_cwd or os.getcwd())
    if not Path(cwd).is_dir():
        err_console.print(f"[red]cwd '{cwd}' does not exist. pass --cwd <path> to override.[/red]")
        sys.exit(1)

    new_args = ["new-session", "-d", "-s", target, "-c", cwd]
    if DEFAULT_TZ:
        new_args.extend(["-e", f"TZ={DEFAULT_TZ}"])
    if engine == "codex":
        new_args.extend(["-e", f"CCTL_WORKSPACE={target}", "-e", f"CCTL_HOME={CCTL_DIR.resolve()}"])
    result = _tmux(*new_args)
    if result.returncode != 0:
        err_console.print(f"[red]tmux new-session failed: {result.stderr.strip()}[/red]")
        sys.exit(1)

    record = Workspace(
        name=target,
        comment=entry.get("comment", ""),
        cwd=cwd,
        tmux_session=target,
        created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        session_id=session_id,
        engine=engine,
    )
    with _locked_store(write=True) as items:
        items.append(record)
    _touch_history([record])

    resume_cmd = _resume_command(binary, engine, session_id)
    _tmux("send-keys", "-t", target, resume_cmd, "Enter")
    _switch_to(target)


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


def _detect_engine(cmd: str) -> str:
    """Leaf name of cmd's first token, if it's a known engine; else ''."""
    if not cmd:
        return ""
    leaf = Path(cmd.split()[0]).name
    return leaf if leaf in KNOWN_ENGINES else ""


def _engine_binary(engine: str) -> str:
    """Map an engine marker back to the binary to invoke (honoring env overrides)."""
    if engine == "codex":
        return DEFAULT_CMD
    if engine == "claude":
        return CLAUDE_CMD
    if engine == "qodercli":
        return QODER_CMD
    return DEFAULT_CMD


def _resume_command(binary: str, engine: str, session_id: str) -> str:
    """Build the engine-specific resume command."""
    if engine == "codex":
        return f"{binary} resume {session_id}" if session_id else f"{binary} resume --last"
    return f"{binary} --resume {session_id}" if session_id else f"{binary} -c"


def _inject_session_id(cmd: str, session_id: str) -> str:
    """Append `--session-id <uuid>` for engines that support caller-assigned IDs."""
    tokens = cmd.split()
    if not tokens:
        return cmd
    leaf = Path(tokens[0]).name
    if leaf not in SESSION_ID_ENGINES:
        return cmd
    if "--session-id" in tokens or "--resume" in tokens:
        return cmd
    return f"{cmd} --session-id {session_id}"


def _engine_cell(engine: str) -> str:
    if not engine:
        return "[dim]-[/dim]"
    style = ENGINE_STYLES.get(engine, "white")
    return f"[{style}]{engine}[/{style}]"


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
