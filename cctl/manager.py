"""Workspace operations shared by CLI and authenticated web handlers."""
from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
from . import storage


def cli():
    from . import cli as module
    return module


def check(result):
    if result.returncode:
        raise click.ClickException(result.stderr.strip() or 'tmux command failed')
    return result.stdout.strip()


def tmux(*args):
    return check(cli()._tmux(*args))


def sessions():
    result = cli()._tmux('list-sessions', '-F', '#{session_name}\t#{session_id}\t#{session_created}')
    if result.returncode:
        if 'no server running' in result.stderr or 'No such file or directory' in result.stderr or 'Connection refused' in result.stderr:
            return {}
        check(result)
    output = {}
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) == 3:
            output[parts[0]] = (parts[1], parts[2])
    return output


def migrate():
    """Back up old records and adopt existing live tmux instances once."""
    c = cli()
    live = sessions()
    with storage.locked(c.STORE_FILE), storage.locked(c.HISTORY_FILE):
        active = storage.read(c.STORE_FILE, [])
        history = storage.read(c.HISTORY_FILE, {})
        by_name = {w['name']: w for w in active}
        for name in set(by_name) | set(history):
            entries = [x for x in (by_name.get(name), history.get(name)) if x is not None]
            wid = next((x.get('workspace_id') for x in entries if x.get('workspace_id')), str(uuid.uuid4()))
            rid = next((x.get('run_id') for x in entries if x.get('run_id')), str(uuid.uuid4()))
            for entry in entries:
                if not entry.get('workspace_id'): entry['workspace_id'] = wid
                if not entry.get('run_id'): entry['run_id'] = rid
                if name in by_name and name in live and not entry.get('tmux_id'):
                    entry['tmux_id'], entry['tmux_created'] = live[name]
        for path, value in ((c.STORE_FILE, active), (c.HISTORY_FILE, history)):
            if path.exists() and storage.read(path, None) != value:
                backup = path.with_name(path.name + '.pre-web.bak')
                if not backup.exists(): storage.atomic(backup, storage.read(path, None))
                storage.atomic(path, value)


def snapshot(history=False):
    c = cli()
    path = c.HISTORY_FILE if history else c.STORE_FILE
    with storage.locked(path):
        raw = storage.read(path, {} if history else [])
        items = list(raw.values()) if history else raw
    try:
        live = sessions()
    except (click.ClickException, OSError, subprocess.SubprocessError):
        live = None
    result = []
    for item in items:
        row = dict(item)
        identity = live.get(row['tmux_session']) if live is not None else None
        matches = identity and identity == (row.get('tmux_id'), row.get('tmux_created', ''))
        row['status'] = 'unknown' if live is None else ('alive' if matches else 'gone')
        result.append(row)
    return result


def workspace(wid, run_id=None):
    for row in snapshot():
        if row.get('workspace_id') == wid:
            if row['status'] != 'alive' or (run_id is not None and row.get('run_id') != run_id):
                raise click.ClickException('Workspace instance changed or is unavailable; refresh the list.')
            return row
    raise KeyError(wid)


def start_workspace(record, command):
    c = cli()
    with c._locked_store(write=True) as items:
        if c._find_by_name(items, record.name) or c._session_exists(record.tmux_session):
            raise click.ClickException('Workspace name is already in use.')
        args = ['new-session', '-d', '-s', record.tmux_session, '-c', record.cwd]
        if c.DEFAULT_TZ: args += ['-e', 'TZ=' + c.DEFAULT_TZ]
        if record.engine == 'codex':
            args += ['-e', 'CCTL_WORKSPACE=' + record.name, '-e', 'CCTL_HOME=' + str(c.CCTL_DIR.resolve()),
                     '-e', 'CCTL_WORKSPACE_ID=' + record.workspace_id, '-e', 'CCTL_RUN_ID=' + record.run_id]
        check(c._tmux(*args))
        identity = sessions().get(record.tmux_session)
        if identity: record.tmux_id, record.tmux_created = identity
        items.append(record)
        c._touch_history([record])
    if command: check(c._tmux('send-keys', '-t', record.tmux_session, command, 'Enter'))
    return record


def restore_workspace(name, new_name=None, cwd_override=None, allow_missing=False, reuse=False):
    c = cli()
    with storage.locked(c.CCTL_DIR / 'restore'):
        entry = c._load_history().get(name)
        if entry is None: raise KeyError(name)
        target = new_name or name
        if reuse:
            for row in snapshot():
                if row['name'] == target and row['status'] == 'alive':
                    return c.Workspace.from_dict(row)
        sid = (entry.get('session_id') or '').strip()
        if not sid and not allow_missing:
            raise click.ClickException('No exact agent session ID. Restore this entry with the local CLI.')
        cwd = str(Path(cwd_override or entry.get('cwd') or os.getcwd()).resolve())
        if not Path(cwd).is_dir():
            raise click.ClickException('Recorded directory is unavailable. Use the local CLI to override it.')
        engine = entry.get('engine') or 'claude'
        if not allow_missing and engine not in c.KNOWN_ENGINES:
            raise click.ClickException('Unknown engine. Use the local CLI.')
        if sid:
            try: uuid.UUID(sid)
            except ValueError: raise click.ClickException('Invalid saved session ID. Use the local CLI.')
        command = c._isolate_codex_command(c._resume_command(c._engine_binary(engine), engine, sid))
        record = c.Workspace(name=target, comment=entry.get('comment', ''), cwd=cwd,
            tmux_session=target, created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
            engine=engine, session_id=sid,
            workspace_id=(entry.get('workspace_id') if not new_name else '') or str(uuid.uuid4()), run_id=str(uuid.uuid4()))
        return start_workspace(record, command)


def structure(row):
    lines = tmux('list-panes', '-s', '-t', row['tmux_id'], '-F',
        '#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_id}\t#{pane_index}\t#{pane_title}\t#{pane_current_command}\t#{pane_active}\t#{window_width}\t#{window_height}')
    windows = {}
    for line in lines.splitlines():
        parts = line.split('\t')
        if len(parts) != 11: continue
        wid, index, name, active, pid, pindex, title, command, pactive, width, height = parts
        window = windows.setdefault(wid, {'id': wid, 'index': int(index), 'name': name, 'active': active == '1',
            'width': int(width), 'height': int(height), 'panes': []})
        window['panes'].append({'id': pid, 'index': int(pindex), 'title': title, 'command': command, 'active': pactive == '1'})
    return sorted(windows.values(), key=lambda w: w['index'])
