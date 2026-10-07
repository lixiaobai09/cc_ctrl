"""Local administrator credentials, revocable browser sessions and recovery journal."""
from __future__ import annotations

import hashlib
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

from .security import Audit


class LoginDenied(Exception):
    pass


class RateLimited(LoginDenied):
    pass


class VerificationBusy(LoginDenied):
    pass


class Auth:
    def __init__(self, root: Path, max_login_concurrency=2):
        self.root = root / 'server'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / 'auth.sqlite3'
        if type(max_login_concurrency) is not int or not 1 <= max_login_concurrency <= 8:
            raise ValueError('Password verification concurrency must be 1–8.')
        self.max_login_concurrency = max_login_concurrency
        self.hasher = PasswordHasher()
        self.audit = Audit(self.root)
        self._login_slots = threading.BoundedSemaphore(max_login_concurrency)
        self._login_pool = ThreadPoolExecutor(max_workers=max_login_concurrency, thread_name_prefix='cct-login')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS admin (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, csrf TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, seen REAL NOT NULL, label TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts (ip TEXT NOT NULL, ts REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_time ON attempts(ts);
                CREATE TABLE IF NOT EXISTS resize_journal (window TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally:
            db.close()

    def configured(self):
        with self.db() as db:
            return db.execute('SELECT 1 FROM admin').fetchone() is not None

    def configure(self, username, password, reset=False):
        if not username or len(username) > 100 or len(password) < 12 or len(password) > 1024:
            raise ValueError('Username is required; password must be 12–1024 characters.')
        hashed = self.hasher.hash(password)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not reset and db.execute('SELECT 1 FROM admin').fetchone():
                raise ValueError('Administrator already configured. Use reset-password.')
            db.execute('INSERT OR REPLACE INTO admin VALUES (1, ?, ?)', (username, hashed))
            db.execute('UPDATE sessions SET revoked=1')
        self.audit.emit('password_reset' if reset else 'admin_initialized', transport='cli')

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _admit_login(self, ip):
        if not self._login_slots.acquire(blocking=False):
            self.audit.emit('login_rejected', reason='verifier_busy', ip=ip, transport='http')
            raise VerificationBusy()

    def login(self, username, password, remember, ip, label):
        self._admit_login(ip)
        try: return self._login(username, password, remember, ip, label)
        finally: self._login_slots.release()

    async def login_async(self, username, password, remember, ip, label):
        # Admission precedes executor submission; overload never builds a queue
        # in asyncio's shared pool used by existing terminal connections.
        self._admit_login(ip)
        try:
            future = self._login_pool.submit(self._login, username, password, remember, ip, label)
        except BaseException:
            self._login_slots.release()
            raise
        # Only actual job completion releases the slot, even if its HTTP request
        # is cancelled. An interrupted client cannot increase hash concurrency.
        future.add_done_callback(lambda completed: self._login_slots.release())
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        return await asyncio.shield(wrapped)

    def close(self):
        self._login_pool.shutdown(wait=True, cancel_futures=True)
        self.audit.flush()

    def _login(self, username, password, remember, ip, label):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE ts < ?', (now - 60,))
            count = db.execute('SELECT COUNT(*) FROM attempts WHERE ip=?', (ip,)).fetchone()[0]
            total = db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
            if count >= 10 or total >= 40:
                self.audit.emit('login_rejected', reason='ip_rate_limit' if count >= 10 else 'global_rate_limit', ip=ip, transport='http')
                raise RateLimited()
            db.execute('INSERT INTO attempts VALUES (?, ?)', (ip, now))
            admin = db.execute('SELECT * FROM admin').fetchone()
        valid = False
        if admin and isinstance(password, str) and len(password) <= 1024:
            try:
                verified = self.hasher.verify(admin['password'], password)
                valid = verified and secrets.compare_digest(str(username).encode(), admin['username'].encode())
            except (VerificationError, InvalidHashError): pass
            except Exception:
                self.audit.emit('login_error', reason='verification_error', ip=ip, transport='http')
                raise
        if not valid:
            self.audit.emit('login_failed', reason='invalid_credentials', ip=ip, transport='http')
            # Bounded delay; endpoint runs this outside the ASGI event loop.
            time.sleep(min(0.2 * (2 ** min(count, 4)), 2))
            raise LoginDenied()
        token, sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        lifetime = 30 * 86400 if remember else 12 * 3600
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Reset-password racing with Argon2 verification cannot issue an old login.
            current = db.execute('SELECT password FROM admin').fetchone()
            if not current or current[0] != admin['password']:
                self.audit.emit('login_failed', reason='credentials_changed', ip=ip, transport='http')
                raise LoginDenied()
            db.execute('DELETE FROM attempts WHERE ip=?', (ip,))
            db.execute('DELETE FROM sessions WHERE expires < ?', (now,))
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, 0)',
                (sid, self.digest(token), csrf, now, now + lifetime, now, label[:200]))
        self.audit.emit('login_success', ip=ip, session_id=sid, transport='http')
        return token, sid

    def session(self, token):
        if not token or len(token) > 200: return None
        now = time.time()
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE token=? AND revoked=0 AND expires>?',
                (self.digest(token), now)).fetchone()
            if row and now - row['seen'] >= 60:
                db.execute('UPDATE sessions SET seen=? WHERE id=?', (now, row['id']))
            return dict(row) if row else None

    def list_sessions(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,created,expires,seen,label FROM sessions WHERE revoked=0 AND expires>? ORDER BY created DESC', (time.time(),))]

    def revoke(self, sid, *, actor_session_id=None, ip=None, logout=False):
        with self.db() as db:
            if sid == '--all': db.execute('UPDATE sessions SET revoked=1 WHERE revoked=0')
            else: db.execute('UPDATE sessions SET revoked=1 WHERE id=? AND revoked=0', (sid,))
            changed = db.total_changes
        event = 'logout' if logout else ('sessions_revoked' if sid == '--all' else 'session_revoked')
        self.audit.emit(event, actor_session_id=actor_session_id, session_id=sid if sid != '--all' and changed else None, ip=ip, count=changed, transport='http' if actor_session_id else 'cli')

    def journal_put(self, window, data):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO resize_journal VALUES (?, ?)', (window, json.dumps(data)))

    def journal_delete(self, window):
        with self.db() as db: db.execute('DELETE FROM resize_journal WHERE window=?', (window,))

    def journal(self):
        with self.db() as db:
            return [json.loads(row['data']) for row in db.execute('SELECT data FROM resize_journal')]
