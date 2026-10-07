"""Same-origin authenticated HTTP and WebSocket application."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import click
import anyio
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, FileResponse
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from .auth import LoginDenied, RateLimited, VerificationBusy
from . import manager
from .terminal import Hub, Client

COOKIE = '__Host-cct_session'
log = logging.getLogger(__name__)


def peer_ip(scope):
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def audit(auth, event, scope, **fields):
    auth.audit.emit(event, ip=peer_ip(scope), transport=scope["type"], **fields)


class Boundary:
    def __init__(self, app, origin, auth):
        self.app, self.origin, self.auth = app, origin, auth
        self.authority = urlsplit(origin).netloc.lower()

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            await self.app(scope, receive, send); return
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode(errors='replace').lower()
        origin = headers.get(b'origin', b'').decode(errors='replace')
        # Browser websockets always supply Origin; require it for every HTTP mutation.
        needs_origin = scope['type'] == 'websocket' or scope.get('method') not in ('GET', 'HEAD', 'OPTIONS')
        invalid = host != self.authority or (needs_origin and origin != self.origin) or (origin and origin != self.origin)
        if scope.get('scheme') not in ('https', 'wss'): invalid = True
        if invalid:
            reason = 'transport' if scope.get('scheme') not in ('https', 'wss') else ('host' if host != self.authority else 'origin')
            event = 'ws_rejected' if scope['type'] == 'websocket' else ('login_rejected' if scope.get('path') == '/api/auth/login' else 'request_rejected')
            audit(self.auth, event, scope, reason=reason)
            if scope['type'] == 'websocket': await send({'type': 'websocket.close', 'code': 4403})
            else: await JSONResponse({'error': 'Invalid request origin or transport.'}, status_code=403)(scope, receive, send)
            return
        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] += [
                    (b'x-content-type-options', b'nosniff'), (b'referrer-policy', b'no-referrer'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
                    (b'cache-control', b'no-store'),
                ]
            await send(message)
        await self.app(scope, receive, secure_send)


async def payload(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536: raise HTTPException(413, 'Request is too large.')
    try:
        data = json.loads(body)
        if not isinstance(data, dict): raise ValueError()
        return data
    except (ValueError, UnicodeDecodeError, RecursionError): raise HTTPException(400, 'Invalid JSON object.')


async def authorize(request, auth, mutation=False):
    session = await asyncio.to_thread(auth.session, request.cookies.get(COOKIE))
    if not session:
        audit(auth, 'request_rejected', request.scope, reason='unauthenticated')
        raise HTTPException(401, 'Please log in.')
    if mutation and not secrets.compare_digest(request.headers.get('x-csrf-token', '').encode(), session['csrf'].encode()):
        audit(auth, 'request_rejected', request.scope, reason='csrf', session_id=session['id'])
        raise HTTPException(403, 'Invalid CSRF token.')
    return session


def create_app(origin, auth):
    hub = Hub(auth)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(manager.migrate)
        await asyncio.to_thread(hub.resize.recover)
        auth.audit.emit('server_started', count=auth.max_login_concurrency, transport='cli')
        try: yield
        finally:
            with anyio.CancelScope(shield=True):
                await hub.shutdown()
                await asyncio.to_thread(auth.close)
                auth.audit.emit('server_stopped', transport='cli')

    async def login(request):
        try: data = await payload(request)
        except HTTPException:
            audit(auth, 'login_rejected', request.scope, reason='invalid_body')
            raise
        if not isinstance(data.get('username'), str) or not isinstance(data.get('password'), str) or type(data.get('remember', False)) is not bool:
            audit(auth, 'login_rejected', request.scope, reason='invalid_fields')
            raise HTTPException(400, 'Invalid login fields.')
        if len(data['username']) > 100 or len(data['password']) > 1024:
            audit(auth, 'login_rejected', request.scope, reason='invalid_fields')
            raise HTTPException(400, 'Login fields are too long.')
        try:
            token, _ = await auth.login_async(data['username'], data['password'], data.get('remember', False),
                peer_ip(request.scope), request.headers.get('user-agent', 'Browser'))
        except VerificationBusy:
            return JSONResponse({'error': 'Password verification is busy. Try again shortly.'}, status_code=429, headers={'Retry-After': '1'})
        except RateLimited:
            return JSONResponse({'error': 'Too many attempts. Try again in one minute.'}, status_code=429, headers={'Retry-After': '60'})
        except LoginDenied: raise HTTPException(401, 'Invalid username or password.')
        except asyncio.CancelledError:
            audit(auth, 'login_error', request.scope, reason='request_cancelled')
            raise
        except Exception:
            audit(auth, 'login_error', request.scope, reason='internal')
            raise HTTPException(503, 'Login temporarily unavailable.')
        response = JSONResponse({'ok': True})
        response.set_cookie(COOKIE, token, max_age=30 * 86400 if data.get('remember') else None,
            secure=True, httponly=True, samesite='strict', path='/')
        return response

    async def current(request):
        session = await authorize(request, auth)
        return JSONResponse({'id': session['id'], 'csrf': session['csrf'], 'expires': session['expires'], 'capabilities': ['pane-scroll']})

    async def logout(request):
        session = await authorize(request, auth, True)
        await asyncio.to_thread(auth.revoke, session['id'], actor_session_id=session['id'], ip=peer_ip(request.scope), logout=True)
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict')
        return response

    async def logins(request):
        await authorize(request, auth)
        return JSONResponse(await asyncio.to_thread(auth.list_sessions))

    async def revoke(request):
        session = await authorize(request, auth, True)
        await asyncio.to_thread(auth.revoke, request.path_params['sid'], actor_session_id=session['id'], ip=peer_ip(request.scope))
        return JSONResponse({'ok': True})

    async def workspaces(request):
        await authorize(request, auth)
        return JSONResponse(await asyncio.to_thread(manager.snapshot))

    async def history(request):
        await authorize(request, auth)
        return JSONResponse(await asyncio.to_thread(manager.snapshot, True))

    async def restore(request):
        await authorize(request, auth, True)
        wid = request.path_params['wid']
        entries = await asyncio.to_thread(manager.snapshot, True)
        entry = next((row for row in entries if row.get('workspace_id') == wid), None)
        if entry is None: raise HTTPException(404, 'History entry not found.')
        record = await asyncio.to_thread(manager.restore_workspace, entry['name'], reuse=True)
        from dataclasses import asdict
        return JSONResponse(asdict(record))

    async def terminal(ws):
        connection_id = secrets.token_urlsafe(12)
        token = ws.cookies.get(COOKIE)
        session = await asyncio.to_thread(auth.session, token)
        if not session:
            audit(auth, 'ws_rejected', ws.scope, reason='unauthenticated', connection_id=connection_id)
            await ws.close(code=4401)
            return
        fields = {'session_id': session['id'], 'connection_id': connection_id}
        run_id = ws.query_params.get('run_id')
        if not run_id:
            audit(auth, 'ws_rejected', ws.scope, reason='missing_instance', **fields)
            await ws.close(code=4409)
            return
        try:
            row = await asyncio.to_thread(manager.workspace, ws.path_params['wid'], run_id)
        except (KeyError, click.ClickException):
            audit(auth, 'ws_rejected', ws.scope, reason='instance_unavailable', **fields)
            await ws.close(code=4409)
            return
        fields['workspace_id'] = row['workspace_id']
        await ws.accept()
        audit(auth, 'ws_open', ws.scope, **fields)
        client = None
        tasks = []
        close_reason, close_code = 'normal', None
        try:
            async with hub.lock:
                key = (row['workspace_id'], row['run_id'])
                first = key not in hub.controllers
                if first and ws.query_params.get('reconnect') != '1':
                    windows = await asyncio.to_thread(manager.structure, row)
                    if windows:
                        await asyncio.to_thread(manager.tmux, 'select-window', '-t', row['tmux_id'] + ':' + windows[0]['id'])
                client = await Client.open(ws, hub, row, token)
                if first: hub.controllers[client.key] = client
            async def receive():
                while True:
                    try: text = await ws.receive_text()
                    except KeyError:
                        client.close_reason = 'invalid_message'
                        audit(auth, 'ws_command_rejected', ws.scope, reason='non_text_message', **fields)
                        await ws.close(code=1003)
                        return
                    if len(text.encode()) > 65536:
                        client.close_reason = 'invalid_message'
                        audit(auth, 'ws_command_rejected', ws.scope, reason='message_size', **fields)
                        await ws.close(code=1009)
                        return
                    if not await asyncio.to_thread(auth.session, token):
                        client.close_reason = 'session_invalidated'
                        await ws.close(code=4401)
                        return
                    try:
                        message = json.loads(text)
                        if not isinstance(message, dict): raise ValueError('Invalid command.')
                    except ValueError:
                        audit(auth, 'ws_command_rejected', ws.scope, reason='invalid_json', **fields)
                        await client.send({'type': 'error', 'message': 'Invalid terminal command.'})
                        continue
                    try: await client.command(message)
                    except (ValueError, KeyError, click.ClickException) as exc:
                        audit(auth, 'ws_command_rejected', ws.scope, reason='command_rejected', **fields)
                        await client.send({'type': 'error', 'message': str(exc)})
            tasks = [asyncio.create_task(f()) for f in (receive, client.output, client.watch, client.authenticate)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            close_reason = client.close_reason
        except WebSocketDisconnect as exc:
            close_code = exc.code
            close_reason = client.close_reason if client and client.close_reason != 'normal' else ('client_disconnect' if exc.code in (1000, 1001) else 'abnormal_disconnect')
            if close_reason == 'abnormal_disconnect':
                audit(auth, 'ws_error', ws.scope, reason=close_reason, close_code=close_code, **fields)
        except (OSError, asyncio.TimeoutError):
            close_reason, close_code = 'transport_error', 1011
            audit(auth, 'ws_error', ws.scope, reason=close_reason, **fields)
            try: await ws.close(code=1011)
            except Exception: pass
        except (KeyError, click.ClickException):
            close_reason, close_code = 'instance_unavailable', 4409
            audit(auth, 'ws_error', ws.scope, reason=close_reason, **fields)
            try: await ws.close(code=4409)
            except Exception: pass
        except asyncio.CancelledError:
            close_reason = 'server_shutdown'
            raise
        except Exception:
            close_reason, close_code = 'internal', 1011
            audit(auth, 'ws_error', ws.scope, reason=close_reason, **fields)
            log.error('Terminal connection failed; see security audit connection ID %s', connection_id)
            try: await ws.close(code=1011)
            except Exception: pass
        finally:
            with anyio.CancelScope(shield=True):
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    if client: await client.close()
                finally:
                    audit(auth, 'ws_closed', ws.scope, reason=close_reason, close_code=close_code, **fields)

    async def exception(request, exc):
        code = exc.status_code if isinstance(exc, HTTPException) else (404 if isinstance(exc, KeyError) else 409)
        detail = exc.detail if isinstance(exc, HTTPException) else ('Not found.' if isinstance(exc, KeyError) else str(exc))
        return JSONResponse({'error': detail}, status_code=code)

    static = Path(__file__).parent / 'static'
    async def index(request):
        if not (static / 'index.html').exists():
            return JSONResponse({'error': 'Web assets missing. Run npm ci and npm run build in web/.'}, status_code=503)
        return FileResponse(static / 'index.html')

    app = Starlette(lifespan=lifespan, routes=[
        Route('/api/auth/login', login, methods=['POST']), Route('/api/auth/session', current),
        Route('/api/auth/logout', logout, methods=['POST']), Route('/api/auth/sessions', logins),
        Route('/api/auth/sessions/{sid}', revoke, methods=['DELETE']),
        Route('/api/workspaces', workspaces), Route('/api/history', history),
        Route('/api/history/{wid}/restore', restore, methods=['POST']),
        WebSocketRoute('/api/workspaces/{wid}/terminal', terminal), Route('/', index),
        Mount('/assets', StaticFiles(directory=static / 'assets', check_dir=False)),
    ], exception_handlers={HTTPException: exception, click.ClickException: exception, KeyError: exception})
    app.state.hub = hub
    return Boundary(app, origin, auth)
