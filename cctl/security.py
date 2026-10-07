"""Bounded security audit trail with an allowlisted, credential-free schema."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class Audit:
    EVENTS = {
        'login_success', 'login_failed', 'login_rejected', 'login_error',
        'admin_initialized', 'password_reset', 'logout', 'session_revoked',
        'sessions_revoked', 'request_rejected', 'ws_rejected', 'ws_open',
        'ws_closed', 'ws_error', 'ws_command_rejected', 'audit_suppressed', 'server_started', 'server_stopped',
    }
    # Never accept exception messages, request bodies, headers, tokens or URLs.
    FIELDS = {'reason', 'ip', 'session_id', 'actor_session_id', 'connection_id',
              'workspace_id', 'transport', 'count', 'close_code'}
    CRITICAL = {'login_success', 'admin_initialized', 'password_reset', 'logout',
                'session_revoked', 'sessions_revoked', 'server_started', 'server_stopped'}

    def __init__(self, root: Path, max_bytes=5 * 1024 * 1024, backups=3, burst=20):
        self.path = root / 'security.jsonl'
        self.lock_path = root / 'security.jsonl.lock'
        self.max_bytes, self.backups, self.burst = max_bytes, backups, burst
        self._mutex = threading.Lock()
        self._second, self._count, self._suppressed = int(time.time()), 0, 0
        with self._file_lock():
            fd = self._open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            os.close(fd)

    @staticmethod
    def _open(path, flags):
        fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.fchmod(fd, 0o600)
        return fd

    @contextmanager
    def _file_lock(self):
        fd = self._open(self.lock_path, os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _record(self, event, fields):
        row = {'time': datetime.now(timezone.utc).isoformat(), 'event': event}
        for key, value in fields.items():
            if value is not None and value != '':
                row[key] = value[:128] if isinstance(value, str) else value
        return row

    def _append(self, records):
        with self._file_lock():
            for record in records:
                line = (json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        source = self.path if index == 1 else Path(str(self.path) + '.' + str(index - 1))
                        target = Path(str(self.path) + '.' + str(index))
                        if source.exists(): os.replace(source, target)
                fd = self._open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                with os.fdopen(fd, 'ab') as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())

    def emit(self, event, **fields):
        if event not in self.EVENTS or fields.keys() - self.FIELDS:
            raise ValueError('Unsupported security audit schema')
        with self._mutex:
            records = []
            second = int(time.time())
            if second != self._second:
                if self._suppressed:
                    records.append(self._record('audit_suppressed', {'reason': 'burst_limit', 'count': self._suppressed}))
                self._second, self._count, self._suppressed = second, 0, 0
            if event not in self.CRITICAL:
                if self._count >= self.burst:
                    self._suppressed += 1
                    if not records: return
                else:
                    self._count += 1
                    records.append(self._record(event, fields))
            else:
                records.append(self._record(event, fields))
            try: self._append(records)
            except OSError as exc:
                # Audit failure never bypasses authentication. Keep service usable
                # for local recovery; report only the error class, never raw data.
                log.error('Security audit write failed (%s)', type(exc).__name__)

    def flush(self):
        with self._mutex:
            if self._suppressed:
                try:
                    self._append([self._record('audit_suppressed', {'reason': 'burst_limit', 'count': self._suppressed})])
                    self._suppressed = 0
                except OSError as exc:
                    log.error('Security audit flush failed (%s)', type(exc).__name__)

    def read(self, limit=50):
        records = deque(maxlen=limit)
        with self._file_lock():
            paths = [Path(str(self.path) + '.' + str(i)) for i in range(self.backups, 0, -1)] + [self.path]
            for path in paths:
                if not path.exists(): continue
                with open(path) as handle:
                    for line in handle:
                        try: records.append(json.loads(line))
                        except json.JSONDecodeError: continue
        return list(reversed(records))
