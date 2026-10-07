from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import shlex
import tempfile
import time
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from cctl import cli, manager, storage
from cctl.auth import Auth, LoginDenied, RateLimited
from cctl.terminal import Resize
from cctl.webapp import COOKIE, create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, value in {'CCTL_DIR': self.root, 'STORE_FILE': self.root/'workspaces.json', 'HISTORY_FILE': self.root/'history.json'}.items():
            self.stack.enter_context(patch.object(cli, key, value))
        self.stack.enter_context(patch.object(manager, 'sessions', return_value={}))
        self.auth = Auth(self.root)
        self.auth.configure('admin', 'a-long-test-password')
        self.client = self.stack.enter_context(TestClient(create_app('https://cct.test', self.auth), base_url='https://cct.test'))
        self.headers = {'Origin': 'https://cct.test'}

    def login(self, remember=False):
        response = self.client.post('/api/auth/login', json={'username':'admin','password':'a-long-test-password','remember':remember}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        session=self.client.get('/api/auth/session').json()
        self.assertIn('pane-scroll',session['capabilities'])
        self.headers['X-CSRF-Token'] = session['csrf']
        return response

    def test_cookie_csrf_and_revocation(self):
        self.assertEqual(self.client.get('/api/workspaces').status_code, 401)
        response = self.login()
        cookie = response.headers['set-cookie']
        for value in ('Secure', 'HttpOnly', 'SameSite=strict', 'Path=/'): self.assertIn(value, cookie)
        self.assertNotIn('Max-Age', cookie)
        self.assertEqual(self.client.post('/api/auth/logout', headers={'Origin':'https://cct.test'}).status_code, 403)
        token = self.client.cookies.get(COOKIE)
        self.assertEqual(self.client.post('/api/auth/logout', headers=self.headers).status_code, 200)
        self.assertIsNone(self.auth.session(token))

    def test_origin_transport_host_and_reset(self):
        self.assertEqual(self.client.post('/api/auth/login', json={}, headers={'Origin':'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('https://evil.test/api/auth/session').status_code, 403)
        self.assertEqual(self.client.get('http://cct.test/api/auth/session').status_code, 403)
        self.login(True)
        self.assertIn('Max-Age=2592000', self.client.post('/api/auth/login', json={'username':'admin','password':'a-long-test-password','remember':True}, headers=self.headers).headers['set-cookie'])
        self.auth.configure('admin','another-long-password',reset=True)
        self.assertEqual(self.client.get('/api/auth/session').status_code, 401)

    def test_expiry_and_rate_limit(self):
        token, _ = self.auth.login('admin','a-long-test-password',False,'local','test')
        with self.auth.db() as db: db.execute('UPDATE sessions SET expires=0')
        self.assertIsNone(self.auth.session(token))
        with patch('cctl.auth.time.sleep'):
            for _ in range(10):
                with self.assertRaises(LoginDenied): self.auth.login('admin','wrong',False,'bad','test')
            with self.assertRaises(RateLimited): self.auth.login('admin','wrong',False,'bad','test')

    def test_query_failure_is_read_only_and_missing_id_not_restored(self):
        self.login()
        entry = {'name':'old','tmux_session':'old','cwd':str(self.root),'workspace_id':'wid','run_id':'rid','engine':'codex','session_id':''}
        storage.atomic(cli.STORE_FILE, [entry]); storage.atomic(cli.HISTORY_FILE, {'old':entry})
        before = cli.STORE_FILE.read_bytes()
        with patch.object(manager, 'sessions', side_effect=OSError('unavailable')):
            response = self.client.get('/api/workspaces')
        self.assertEqual(response.json()[0]['status'], 'unknown')
        self.assertEqual(cli.STORE_FILE.read_bytes(), before)
        with patch.object(cli, '_tmux') as tmux:
            response = self.client.post('/api/history/wid/restore', json={}, headers=self.headers)
            self.assertEqual(response.status_code, 409)
            tmux.assert_not_called()

    def test_hook_rejects_old_run_and_static_resources(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        record = cli.Workspace('a','','/tmp','a','now',engine='codex',workspace_id='wid',run_id='new')
        storage.atomic(cli.STORE_FILE, [record.__dict__])
        with patch.object(cli, '_live_sessions', return_value={'a'}):
            self.assertFalse(cli._capture_codex_session({'hook_event_name':'SessionStart','session_id':str(uuid.uuid4()),'cwd':'/tmp','transcript_path':'/tmp/log'}, 'a','wid','old'))
        self.assertEqual(json.loads(cli.STORE_FILE.read_text())[0]['session_id'], '')


@unittest.skipUnless(shutil.which('tmux'), 'tmux required')
class TmuxTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.socket = str(self.root/'tmux.sock')
        self.stack.enter_context(patch.dict(os.environ, {'CCTL_TMUX_SOCKET':self.socket}))
        for key, value in {'CCTL_DIR':self.root,'STORE_FILE':self.root/'workspaces.json','HISTORY_FILE':self.root/'history.json','DEFAULT_CMD':'/bin/echo'}.items():
            self.stack.enter_context(patch.object(cli,key,value))
        result = subprocess.run(['tmux','-S',self.socket,'-f','/dev/null','new-session','-d','-s','test','-x','120','-y','40','/bin/cat'], capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: subprocess.run(['tmux','-S',self.socket,'kill-server'], capture_output=True))
        manager.tmux('set-option','-g','default-shell','/bin/sh')
        manager.tmux('new-window','-t','test:1','-n','second','/bin/cat')
        manager.tmux('split-window','-t','test:1','-h','/bin/cat')
        entry={'name':'test','comment':'','cwd':str(self.root),'tmux_session':'test','created_at':'now','engine':'codex','session_id':str(uuid.uuid4())}
        storage.atomic(cli.STORE_FILE,[entry]);storage.atomic(cli.HISTORY_FILE,{'test':entry})
        manager.migrate();self.row=manager.snapshot()[0]
        self.auth=Auth(self.root);self.auth.configure('admin','a-long-test-password')

    def client(self):
        client=self.stack.enter_context(TestClient(create_app('https://cct.test',self.auth),base_url='https://cct.test'))
        response=client.post('/api/auth/login',json={'username':'admin','password':'a-long-test-password'},headers={'Origin':'https://cct.test'})
        self.assertEqual(response.status_code,200)
        return client

    def test_migration_resize_recovery_and_external_change(self):
        self.assertTrue(cli.STORE_FILE.with_name('workspaces.json.pre-web.bak').exists())
        before=manager.snapshot()[0]['workspace_id'];manager.migrate();self.assertEqual(manager.snapshot()[0]['workspace_id'],before)
        resize=Resize(self.auth);window=next(w['id'] for w in manager.structure(self.row) if w['active'])
        original=resize.state(window)
        resize.enable(self.row,window,45,20)
        self.assertEqual(resize.state(window)['width'],45)
        resize.recover();self.assertEqual(resize.state(window)['local'],original['local'])
        resize.enable(self.row,window,50,22)
        manager.tmux('resize-window','-t',window,'-x','90','-y','30')
        resize.recover();self.assertEqual(resize.state(window)['width'],90)
        self.assertEqual(self.auth.journal(),[])
        manager.tmux('set-option','-w','-t',window,'window-size','manual')
        original=resize.state(window)
        resize.enable(self.row,window,40,20);resize.recover()
        self.assertEqual(resize.state(window),original)

    def test_websocket_selection_input_adaptation_and_revoke(self):
        client=self.client()
        url=f'wss://cct.test/api/workspaces/{self.row["workspace_id"]}/terminal?run_id={self.row["run_id"]}'
        with client.websocket_connect(url,headers={'Origin':'https://cct.test'}) as ws:
            state=self.state(ws)
            self.assertTrue(state['writer'])
            self.assertEqual(next(w['index'] for w in state['windows'] if w['active']),0)
            ws.send_json({'type':'input','data':'CCT_TEST_MARKER\r'})
            found=False
            for _ in range(30):
                event=ws.receive()
                if event.get('bytes') and b'CCT_TEST_MARKER' in event['bytes']: found=True;break
            self.assertTrue(found)
            second=next(w for w in state['windows'] if w['index']==1)
            ws.send_json({'type':'select-pane','id':second['panes'][0]['id']})
            state=self.state(ws)
            self.assertEqual(next(w['index'] for w in state['windows'] if w['active']),1)
            ws.send_json({'type':'adapt','window':state['windows'][0]['id'],'cols':48,'rows':22})
            time.sleep(.15)
            self.assertEqual(self.auth.journal(),[])
            ws.send_json({'type':'adapt','window':second['id'],'cols':48,'rows':22})
            state=self.state(ws)
            self.assertTrue(state['adapted'])
            self.assertEqual(next(w['width'] for w in state['windows'] if w['active']),48)
            clients=manager.tmux('list-clients','-F','#{client_width},#{client_height}')
            self.assertIn('48,22',clients)
            self.auth.revoke('--all')
            for _ in range(30):
                event=ws.receive()
                if event['type']=='websocket.close':
                    self.assertEqual(event['code'],4401);break
            else:self.fail('Revoked websocket stayed open')
        deadline=time.monotonic()+3
        while self.auth.journal() and time.monotonic()<deadline: time.sleep(0.02)
        self.assertEqual(self.auth.journal(),[])
        self.assertIn('test',manager.sessions())

    @staticmethod
    def state(ws, predicate=lambda state: True):
        for _ in range(100):
            event=ws.receive()
            if event['type']=='websocket.close': raise AssertionError(event)
            if event.get('text'):
                data=json.loads(event['text'])
                if data['type']=='error': raise AssertionError(data)
                if data['type']=='state' and predicate(data): return data
        raise AssertionError('No terminal state')

    def test_multiple_browsers_takeover_and_reconnect(self):
        first=self.client(); second=first
        url=f'wss://cct.test/api/workspaces/{self.row["workspace_id"]}/terminal?run_id={self.row["run_id"]}'
        with first.websocket_connect(url,headers={'Origin':'https://cct.test'}) as a:
            initial=self.state(a)
            current=next(w for w in initial['windows'] if w['active'])
            first_size=(current['width'],current['height'])
            with second.websocket_connect(url,headers={'Origin':'https://cct.test'}) as b:
                self.assertFalse(self.state(b)['writer'])
                b.send_json({'type':'input','data':'FORBIDDEN'})
                for _ in range(100):
                    event=b.receive()
                    if event.get('text'):
                        data=json.loads(event['text'])
                        if data['type']=='error': break
                else: self.fail('Observer input was accepted')
                a.send_json({'type':'adapt','cols':40,'rows':20})
                self.assertTrue(self.state(a)['adapted'])
                b.send_json({'type':'claim'})
                self.assertTrue(self.state(b,lambda state:state['writer'])['writer'])
                self.assertEqual(self.auth.journal(),[])
                deadline=time.monotonic()+2
                target=manager.structure(self.row)[0]
                while (target['width'],target['height']) != first_size and time.monotonic()<deadline:
                    time.sleep(.02);target=manager.structure(self.row)[0]
                self.assertEqual((target['width'],target['height']),first_size, manager.tmux('list-clients','-F','#{client_tty}: #{client_width},#{client_height},#{client_flags}') + str(Resize(self.auth).state(target['id'])))
                window=next(w for w in manager.structure(self.row) if w['index']==1)
                b.send_json({'type':'select-window','id':window['id']})
                state=self.state(b)
                self.assertEqual(next(w['index'] for w in state['windows'] if w['active']),1)
        with first.websocket_connect(url+'&reconnect=1',headers={'Origin':'https://cct.test'}) as reconnected:
            self.assertEqual(next(w['index'] for w in self.state(reconnected)['windows'] if w['active']),1)
        self.assertIn('test',manager.sessions())

    def test_scroll_for_mouse_aware_app(self):
        script=self.root/'mouse_app.py'; output=self.root/'wheel-events'
        script.write_text("import os,tty\ntty.setraw(0)\nos.write(1,b'\\x1b[?1000h\\x1b[?1006h')\nwhile True:\n data=os.read(0,4096)\n if not data:break\n with open("+repr(str(output))+",'ab') as handle:handle.write(data)\n")
        manager.tmux('new-window','-t','test:3','-n','mouse-test',shlex.quote(sys.executable)+' '+shlex.quote(str(script)))
        deadline=time.monotonic()+3
        while manager.tmux('display-message','-p','-t','test:3','#{mouse_sgr_flag}')!='1' and time.monotonic()<deadline:time.sleep(.02)
        client=self.client()
        url=f'wss://cct.test/api/workspaces/{self.row["workspace_id"]}/terminal?run_id={self.row["run_id"]}'
        with client.websocket_connect(url,headers={'Origin':'https://cct.test'}) as ws:
            state=self.state(ws); target=next(w for w in state['windows'] if w['index']==3)
            ws.send_json({'type':'select-window','id':target['id']})
            state=self.state(ws,lambda state:any(w['index']==3 and w['active'] for w in state['windows']))
            self.assertEqual(next(w['index'] for w in state['windows'] if w['active']),3)
            ws.send_json({'type':'scroll','window':target['id'],'pane':target['panes'][0]['id'],'lines':-3})
            deadline=time.monotonic()+3
            while (not output.exists() or output.read_bytes().count(b'\x1b[<64;')<3) and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(output.read_bytes().count(b'\x1b[<64;'),3)
            ws.send_json({'type':'scroll','window':target['id'],'pane':target['panes'][0]['id'],'lines':2})
            deadline=time.monotonic()+3
            while output.read_bytes().count(b'\x1b[<65;')<2 and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(output.read_bytes().count(b'\x1b[<65;'),2)
            self.assertEqual(manager.tmux('display-message','-p','-t','test:3','#{pane_mode}'),'')
            self.assertEqual(manager.tmux('show-options','-g','-v','mouse'),'off')

    def test_stale_instance_and_restore(self):
        client=self.client()
        with self.assertRaises(WebSocketDisconnect) as denied:
            with client.websocket_connect(f'wss://cct.test/api/workspaces/{self.row["workspace_id"]}/terminal?run_id=stale',headers={'Origin':'https://cct.test'}): pass
        self.assertEqual(denied.exception.code,4409)
        manager.tmux('kill-session','-t','test')
        record=manager.restore_workspace('test',reuse=True)
        self.assertNotEqual(record.run_id,self.row['run_id'])
        self.assertEqual(record.workspace_id,self.row['workspace_id'])
        self.assertEqual(manager.restore_workspace('test',reuse=True).run_id,record.run_id)
