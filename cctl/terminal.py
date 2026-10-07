"""PTY clients, shared writer leases and reversible window sizing."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import struct
import subprocess
import termios

import anyio

from . import manager

log = logging.getLogger(__name__)


async def mutation(function, *args):
    """Finish a tmux mutation before a cancelled connection can restore/close it."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class Resize:
    def __init__(self, auth):
        self.auth = auth

    def state(self, window):
        local = manager.tmux('show-options', '-w', '-v', '-t', window, 'window-size')
        effective = manager.tmux('show-options', '-w', '-A', '-v', '-t', window, 'window-size')
        size = manager.tmux('display-message', '-p', '-t', window, '#{window_width},#{window_height}')
        width, height = map(int, size.split(','))
        return {'local': local, 'effective': effective, 'width': width, 'height': height}

    def enable(self, row, window, cols, rows):
        windows = manager.structure(row)
        target = next((w for w in windows if w['id'] == window and w['active']), None)
        if target is None: raise ValueError('Selected window changed; try again.')
        records = {r['window']: r for r in self.auth.journal()}
        existing = records.get(window)
        current = self.state(window)
        if existing and (existing['workspace_id'] != row['workspace_id'] or existing['run_id'] != row['run_id']):
            raise ValueError('Another window resize recovery is pending.')
        if existing and current != existing['expected']:
            self.auth.journal_delete(window)
            raise ValueError('Window size changed outside the web client. Enable adaptation again.')
        record = existing or {'window': window, 'workspace_id': row['workspace_id'], 'run_id': row['run_id'],
            'tmux_id': row['tmux_id'], 'tmux_created': row['tmux_created'],
            'socket': os.environ.get('CCTL_TMUX_SOCKET', ''), 'original': current}
        # tmux status lines consume rows in the attached terminal.
        status = manager.tmux('show-options', '-v', '-t', row['tmux_id'], 'status')
        status_rows = 0 if status == 'off' else (int(status) if status.isdigit() else 1)
        height = max(2, rows - status_rows)
        record['expected'] = {'local': 'manual', 'effective': 'manual', 'width': cols, 'height': height}
        self.auth.journal_put(window, record)  # Intent is durable before changing tmux.
        manager.tmux('resize-window', '-t', window, '-x', str(cols), '-y', str(height))
        record['expected'] = self.state(window)  # tmux may clamp dimensions for many panes.
        self.auth.journal_put(window, record)
        return record['expected']

    def restore(self, window):
        record = next((r for r in self.auth.journal() if r['window'] == window), None)
        if not record: return
        if record['socket'] != os.environ.get('CCTL_TMUX_SOCKET', ''):
            raise ValueError('Resize journal belongs to a different tmux socket.')
        live = manager.sessions()
        if (record['tmux_id'], record['tmux_created']) not in live.values():
            self.auth.journal_delete(window)
            return
        row = {'tmux_id': record['tmux_id']}
        if window not in {w['id'] for w in manager.structure(row)}:
            self.auth.journal_delete(window)
            return
        current = self.state(window)
        if current != record['expected']:
            log.warning('Window %s changed independently; preserving its current size', window)
            self.auth.journal_delete(window)
            return
        original = record['original']
        if original['effective'] == 'manual':
            manager.tmux('resize-window', '-t', window, '-x', str(original['width']), '-y', str(original['height']))
        if original['local']:
            manager.tmux('set-option', '-w', '-t', window, 'window-size', original['local'])
        else:
            manager.tmux('set-option', '-w', '-u', '-t', window, 'window-size')
        self.auth.journal_delete(window)

    def recover(self):
        for row in self.auth.journal(): self.restore(row['window'])


class Pty:
    def __init__(self, row):
        windows = manager.structure(row)
        active = next(w for w in windows if w['active'])
        status = manager.tmux('show-options', '-v', '-t', row['tmux_id'], 'status')
        status_rows = 0 if status == 'off' else (int(status) if status.isdigit() else 1)
        self.master, slave = os.openpty()
        env = dict(os.environ, TERM='xterm-256color')
        for name in ('TMUX', 'TMUX_PANE', 'CCTL_WORKSPACE', 'CCTL_WORKSPACE_ID', 'CCTL_RUN_ID'):
            env.pop(name, None)
        socket = os.environ.get('CCTL_TMUX_SOCKET')
        command = ['tmux'] + (['-S', socket] if socket else [])
        command += ['attach-session', '-f', 'ignore-size', '-t', row['tmux_id']]
        try:
            self.size(active['width'], active['height'] + status_rows)
            self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                env=env, start_new_session=True, close_fds=True)
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(self.master, False)
        self.closed = False

    def listen(self):
        # Python 3.9 binds Queue to a loop at construction; create it on ASGI thread.
        self.queue = asyncio.Queue(maxsize=128)
        asyncio.get_running_loop().add_reader(self.master, self.read)

    def size(self, cols, rows):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        if hasattr(self, 'process') and self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGWINCH)

    def read(self):
        try:
            data = os.read(self.master, 4096)
            if not data: self.eof(); return
            self.queue.put_nowait(data)
        except BlockingIOError: pass
        except (OSError, asyncio.QueueFull): self.eof()

    def eof(self):
        asyncio.get_running_loop().remove_reader(self.master)
        # Reserve a termination notification even for a slow browser.
        if self.queue.full(): self.queue.get_nowait()
        self.queue.put_nowait(None)

    async def write(self, data):
        view = memoryview(data)
        while view:
            try:
                count = os.write(self.master, view)
                view = view[count:]
            except BlockingIOError:
                loop = asyncio.get_running_loop()
                ready = loop.create_future()
                def wake():
                    if not ready.done(): ready.set_result(None)
                loop.add_writer(self.master, wake)
                try: await asyncio.wait_for(ready, 2)
                finally: loop.remove_writer(self.master)

    async def close(self):
        if self.closed: return
        self.closed = True
        loop = asyncio.get_running_loop()
        loop.remove_reader(self.master)
        loop.remove_writer(self.master)
        os.close(self.master)
        if self.process.poll() is None:
            self.process.terminate()  # Only this tmux client, never the server/agent.
            try: await asyncio.wait_for(asyncio.to_thread(self.process.wait), 2)
            except asyncio.TimeoutError:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)


class Hub:
    def __init__(self, auth):
        self.auth = auth
        self.resize = Resize(auth)
        self.controllers = {}
        self.clients = set()
        self._lock = None

    @property
    def lock(self):
        # create_app may run outside ASGI's loop (notably TestClient/Python 3.9).
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def shutdown(self):
        with anyio.CancelScope(shield=True):
            for client in list(self.clients): await client.close()
            await asyncio.to_thread(self.resize.recover)

    async def release(self, client):
        async with self.lock:
            if self.controllers.get(client.key) is client:
                if client.adapted:
                    await client.restore_adaptation()
                self.controllers.pop(client.key, None)

    async def claim(self, client):
        async with self.lock:
            old = self.controllers.get(client.key)
            if old is client: return
            if old and old.adapted:
                await old.restore_adaptation()
            self.controllers[client.key] = client
            if old: await old.send({'type': 'control', 'writer': False, 'adapted': False})
            await client.send({'type': 'control', 'writer': True, 'adapted': False})


class Client:
    def __init__(self, ws, hub, row, token, pty):
        self.ws, self.hub, self.row, self.token = ws, hub, row, token
        self.key = (row['workspace_id'], row['run_id'])
        self.pty = pty
        self.pty.listen()
        self.adapted = None
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.last_state = None
        self.hub.clients.add(self)

    @classmethod
    async def open(cls, ws, hub, row, token):
        # tmux startup/query latency must not block other clients' login checks.
        task = asyncio.create_task(asyncio.to_thread(Pty, row))
        try:
            pty = await asyncio.shield(task)
        except asyncio.CancelledError:
            pty = await task
            await pty.close()
            raise
        return cls(ws, hub, row, token, pty)

    async def send(self, data):
        async with self.send_lock:
            if isinstance(data, bytes): await asyncio.wait_for(self.ws.send_bytes(data), 5)
            else: await asyncio.wait_for(self.ws.send_json(data), 5)

    async def output(self):
        while True:
            data = await self.pty.queue.get()
            if data is None: return
            await self.send(data)

    async def authenticate(self):
        while True:
            if not await asyncio.to_thread(self.hub.auth.session, self.token):
                await self.ws.close(code=4401)
                return
            await asyncio.sleep(1)

    async def watch(self):
        while True:
            # Serialize snapshots with selection/resize, so stale geometry cannot
            # undo a newer window selection or trigger another automatic resize.
            async with self.hub.lock:
                row = await asyncio.to_thread(manager.workspace, *self.key)
                windows = await asyncio.to_thread(manager.structure, row)
                active = next((w['id'] for w in windows if w['active']), None)
                if self.adapted and active != self.adapted:
                    await self.restore_adaptation()
                state = {'type': 'state', 'windows': windows,
                    'writer': self.hub.controllers.get(self.key) is self, 'adapted': bool(self.adapted),
                    'status_rows': await asyncio.to_thread(self.status_rows, row)}
                if state != self.last_state:
                    await self.send(state)
                    self.last_state = state
            await asyncio.sleep(1)

    @staticmethod
    def status_rows(row):
        status = manager.tmux('show-options', '-v', '-t', row['tmux_id'], 'status')
        return 0 if status == 'off' else (int(status) if status.isdigit() else 1)

    async def restore_adaptation(self):
        if not self.adapted: return
        record = next((r for r in self.hub.auth.journal() if r['window'] == self.adapted), None)
        if record:
            original = record['original']
            status = await asyncio.to_thread(self.status_rows, self.row)
            self.pty.size(original['width'], original['height'] + status)
        await mutation(self.hub.resize.restore, self.adapted)
        self.adapted = None

    async def command(self, message):
        kind = message.get('type')
        if kind == 'claim': await self.hub.claim(self); return
        if kind == 'release': await self.hub.release(self); return
        async with self.hub.lock:
            row = await asyncio.to_thread(manager.workspace, *self.key)
            if kind == 'viewport':
                cols, rows = self.dimensions(message)
                self.pty.size(cols, rows)
                return
            if self.hub.controllers.get(self.key) is not self:
                raise ValueError('Take control before sending input or changing windows.')
            if kind == 'input':
                data = message.get('data')
                if not isinstance(data, str) or len(data.encode()) > 16384:
                    raise ValueError('Input is limited to 16 KiB.')
                await self.pty.write(data.encode())
            elif kind == 'scroll':
                lines = message.get('lines')
                if type(lines) is not int or not -50 <= lines <= 50:
                    raise ValueError('Invalid scroll distance.')
                if not lines: return
                windows = await asyncio.to_thread(manager.structure, row)
                window = next(w for w in windows if w['active'])
                pane = next(p for p in window['panes'] if p['active'])
                if message.get('window') != window['id'] or message.get('pane') != pane['id']:
                    return  # Never scroll a newly selected target with an old gesture.
                await mutation(self.scroll_pane, pane['id'], lines)
            elif kind in ('select-window', 'select-pane'):
                windows = await asyncio.to_thread(manager.structure, row)
                target = message.get('id')
                if kind == 'select-window':
                    window = next((w for w in windows if w['id'] == target), None)
                else:
                    window = next((w for w in windows if any(p['id'] == target for p in w['panes'])), None)
                if window is None: raise ValueError('Target closed. Refresh the window list.')
                if self.adapted:
                    await self.restore_adaptation()
                await mutation(manager.tmux, 'select-window', '-t', row['tmux_id'] + ':' + window['id'])
                if kind == 'select-pane': await mutation(manager.tmux, 'select-pane', '-t', target)
            elif kind == 'adapt':
                cols, rows = self.dimensions(message)
                windows = await asyncio.to_thread(manager.structure, row)
                window = next(w['id'] for w in windows if w['active'])
                if message.get('window') is not None and message['window'] != window:
                    return  # A viewport/font update queued before switching windows.
                if self.adapted and self.adapted != window:
                    await self.restore_adaptation()
                self.adapted = window  # Ensure cleanup runs even if resize fails midway.
                # Journal original geometry before SIGWINCH can change an automatic window.
                actual = await mutation(self.hub.resize.enable, row, window, cols, rows)
                self.pty.size(cols, rows)
                await self.send({'type': 'adapted', 'width': actual['width'], 'height': actual['height']})
            elif kind == 'restore-size':
                if self.adapted:
                    await self.restore_adaptation()
            else: raise ValueError('Unknown terminal command.')

    @staticmethod
    def scroll_pane(pane, lines):
        values = manager.tmux('display-message', '-p', '-t', pane,
            'M#{pane_mode}\t#{mouse_any_flag}\t#{mouse_sgr_flag}\t#{pane_width}\t#{pane_height}').split('\t')
        mode, mouse, sgr, width, height = values
        mode = mode[1:]  # Sentinel preserves an empty first field through stdout.strip().
        if mode and mode != 'copy-mode':
            raise ValueError('Exit the current pane mode before scrolling.')
        if not mode and mouse == '1':
            # Forward actual wheel events directly to the mouse-aware app, without
            # changing the user's tmux mouse settings or injecting arrow keys.
            x, y = max(1, int(width)//2), max(1, int(height)//2)
            button = 64 if lines < 0 else 65
            if sgr == '1': event = f'\x1b[<{button};{x};{y}M'
            else: event = '\x1b[M' + chr(button+32) + chr(min(x,95)+32) + chr(min(y,95)+32)
            manager.tmux('send-keys', '-l', '-t', pane, event * abs(lines))
            return
        if not mode:
            if lines > 0: return
            manager.tmux('copy-mode', '-e', '-t', pane)
        manager.tmux('send-keys', '-X', '-N', str(abs(lines)), '-t', pane,
            'scroll-up' if lines < 0 else 'scroll-down')

    @staticmethod
    def dimensions(message):
        cols, rows = message.get('cols'), message.get('rows')
        if type(cols) is not int or type(rows) is not int or not (10 <= cols <= 500 and 5 <= rows <= 300):
            raise ValueError('Invalid terminal dimensions.')
        return cols, rows

    async def close(self):
        if self.closed: return
        self.closed = True
        try: await self.hub.release(self)
        finally:
            await self.pty.close()
            self.hub.clients.discard(self)
            try: await self.ws.close()
            except Exception: pass
