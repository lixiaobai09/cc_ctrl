from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from cctl import cli, manager
from cctl.auth import Auth, LoginDenied, VerificationBusy
from cctl.security import Audit
from cctl.webapp import COOKIE, create_app


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.auth = Auth(self.root, max_login_concurrency=1)
        self.stack.callback(self.auth.close)
        self.auth.configure('admin', 'security-test-password')
        for key, value in {'CCTL_DIR': self.root, 'STORE_FILE': self.root/'workspaces.json', 'HISTORY_FILE': self.root/'history.json'}.items():
            self.stack.enter_context(patch.object(cli, key, value))
        self.stack.enter_context(patch.object(manager, 'sessions', return_value={}))

    def test_http_admission_rejects_before_another_verification_and_recovers(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.auth.hasher.verify
        def blocked(*args):
            entered.set()
            if not finish.wait(5): raise AssertionError('Test verification never released')
            return original(*args)
        with patch.object(self.auth, 'hasher') as verifier:
            verifier.verify.side_effect=blocked
            with TestClient(create_app('https://cct.test', self.auth), base_url='https://cct.test') as client:
                body = {'username':'admin', 'password':'security-test-password'}
                headers = {'Origin':'https://cct.test'}
                with ThreadPoolExecutor(max_workers=1) as requests:
                    first = requests.submit(client.post, '/api/auth/login', json=body, headers=headers)
                    try:
                        self.assertTrue(entered.wait(2))
                        second = client.post('/api/auth/login', json=body, headers=headers)
                        self.assertEqual(second.status_code, 429)
                        self.assertEqual(second.headers['retry-after'], '1')
                        self.assertEqual(self.auth.hasher.verify.call_count, 1)
                        # Busy logins do not reach SQLite rate-counting or hashing.
                        with self.auth.db() as db:
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
                    finally:
                        finish.set()
                    self.assertEqual(first.result(timeout=3).status_code, 200)
                    self.assertEqual(client.post('/api/auth/login', json=body, headers=headers).status_code, 200)
        events = self.auth.audit.read(50)
        self.assertTrue(any(e['event']=='login_rejected' and e.get('reason')=='verifier_busy' for e in events))

    def test_cancellation_does_not_release_slot_while_hash_is_running(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.auth.hasher.verify
        def blocked(*args):
            entered.set()
            if not finish.wait(5): raise AssertionError('Test verification never released')
            return original(*args)
        async def run():
            args = ('admin', 'security-test-password', False, '127.0.0.1', 'browser')
            first = asyncio.create_task(self.auth.login_async(*args))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError): await first
            with self.assertRaises(VerificationBusy): await self.auth.login_async(*args)
            finish.set()
            # Wait for the real worker and its completion callback, not the cancelled request.
            for _ in range(100):
                try:
                    await self.auth.login_async(*args)
                    break
                except VerificationBusy: await asyncio.sleep(.02)
            else: self.fail('Verification slot was not released after job completion')
        try:
            with patch.object(self.auth, 'hasher') as verifier:
                verifier.verify.side_effect=blocked
                asyncio.run(run())
        finally: finish.set()

    def test_exception_releases_slot_and_audit_never_contains_error_or_credentials(self):
        with patch.object(self.auth, 'hasher') as verifier:
            verifier.verify.side_effect=RuntimeError('SECRET_EXCEPTION_TEXT')
            with self.assertRaises(RuntimeError): self.auth.login('SECRET_USERNAME', 'SECRET_PASSWORD', False, '127.0.0.1', 'SECRET_USER_AGENT')
        token, sid = self.auth.login('admin', 'security-test-password', False, '127.0.0.1', 'SECRET_USER_AGENT')
        self.auth.revoke(sid, actor_session_id=sid, ip='127.0.0.1')
        rows = self.auth.audit.read(50)
        text = json.dumps(rows)
        for secret in ('SECRET_EXCEPTION_TEXT', 'SECRET_USERNAME', 'SECRET_PASSWORD', 'SECRET_USER_AGENT', 'security-test-password', token, self.auth.digest(token)):
            self.assertNotIn(secret, text)
        for event in ('login_error', 'login_success', 'session_revoked'):
            self.assertTrue(any(row['event']==event for row in rows))
        self.assertEqual(stat.S_IMODE(self.auth.audit.path.stat().st_mode), 0o600)

    def test_http_and_websocket_audit_rejections_and_logout(self):
        with TestClient(create_app('https://cct.test', self.auth), base_url='https://cct.test') as client:
            with patch('cctl.auth.time.sleep'):
                response = client.post('/api/auth/login', json={'username':'SECRET_USERNAME','password':'SECRET_PASSWORD'}, headers={'Origin':'https://cct.test'})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(client.post('/api/auth/login', content='SECRET_BAD_JSON', headers={'Origin':'https://cct.test'}).status_code, 400)
            self.assertEqual(client.post('/api/auth/login', json={}, headers={'Origin':'https://evil.test'}).status_code, 403)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('wss://cct.test/api/workspaces/SECRET_PATH/terminal?run_id=SECRET_QUERY', headers={'Origin':'https://cct.test'}): pass
            client.post('/api/auth/login', json={'username':'admin','password':'security-test-password'}, headers={'Origin':'https://cct.test'})
            token = client.cookies.get(COOKIE)
            session = client.get('/api/auth/session').json()
            self.assertEqual(client.post('/api/auth/logout', headers={'Origin':'https://cct.test', 'X-CSRF-Token':'SECRET_CSRF'}).status_code, 403)
            self.assertEqual(client.post('/api/auth/logout', headers={'Origin':'https://cct.test', 'X-CSRF-Token':session['csrf']}).status_code, 200)
        events = self.auth.audit.read(50)
        for event in ('login_failed', 'login_rejected', 'login_success', 'ws_rejected', 'request_rejected', 'logout'):
            self.assertTrue(any(row['event']==event for row in events), event)
        text = json.dumps(events)
        for secret in ('SECRET_USERNAME', 'SECRET_PASSWORD', 'SECRET_BAD_JSON', 'SECRET_PATH', 'SECRET_QUERY', 'SECRET_CSRF', token, session['csrf']):
            self.assertNotIn(secret, text)

    def test_connection_error_is_audited_without_error_message(self):
        with TestClient(create_app('https://cct.test', self.auth), base_url='https://cct.test') as client:
            client.post('/api/auth/login', json={'username':'admin','password':'security-test-password'}, headers={'Origin':'https://cct.test'})
            row = {'workspace_id':'trusted-workspace', 'run_id':'run-id', 'tmux_id':'$0'}
            with patch.object(manager, 'workspace', return_value=row), patch.object(manager, 'structure', return_value=[]), patch('cctl.webapp.Client.open', side_effect=OSError('SECRET_IO_ERROR')):
                with client.websocket_connect('wss://cct.test/api/workspaces/trusted-workspace/terminal?run_id=run-id',headers={'Origin':'https://cct.test'}) as ws:
                    event = ws.receive()
                    self.assertEqual(event['type'],'websocket.close')
                    self.assertEqual(event['code'],1011)
        rows=self.auth.audit.read(50)
        for event in ('ws_open','ws_error','ws_closed'):
            self.assertTrue(any(row['event']==event for row in rows))
        self.assertNotIn('SECRET_IO_ERROR',json.dumps(rows))
        self.assertTrue(any(row.get('reason')=='transport_error' for row in rows))

    def test_rotated_logs_are_bounded_and_bursts_are_summarized(self):
        audit = Audit(self.auth.root, max_bytes=500, backups=2, burst=2)
        with patch('cctl.security.time.time', return_value=100):
            for _ in range(20): audit.emit('login_rejected', reason='verifier_busy', ip='127.0.0.1')
        audit.flush()
        rows = audit.read(100)
        self.assertEqual(sum(r.get('count',0) for r in rows if r['event']=='audit_suppressed'),18)
        for _ in range(20): audit.emit('login_success', session_id='test-id', ip='127.0.0.1')
        paths = list(self.auth.root.glob('security.jsonl*'))
        self.assertTrue((self.auth.root/'security.jsonl.2').exists())
        self.assertFalse((self.auth.root/'security.jsonl.3').exists())
        for path in paths:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            if path.name.endswith('.lock'): continue
            self.assertLessEqual(path.stat().st_size,500)
            for line in path.read_text().splitlines(): self.assertIsInstance(json.loads(line),dict)
