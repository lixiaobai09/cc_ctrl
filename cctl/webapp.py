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

from .auth import LoginDenied, RateLimited
from . import manager
from .terminal import Hub, Client

COOKIE = '__Host-cct_session'
log = logging.getLogger(__name__)


class Boundary:
    def __init__(self, app, origin):
        self.app, self.origin = app, origin
        self.authority = urlsplit(origin).netloc.lower()

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            await self.app(scope, receive, send); return
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode().lower()
        origin = headers.get(b'origin', b'').decode()
        # Browser websockets always supply Origin; require it for every HTTP mutation.
        needs_origin = scope['type'] == 'websocket' or scope.get('method') not in ('GET', 'HEAD', 'OPTIONS')
        invalid = host != self.authority or (needs_origin and origin != self.origin) or (origin and origin != self.origin)
        if scope.get('scheme') not in ('https', 'wss'): invalid = True
        if invalid:
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
    except (ValueError, UnicodeDecodeError): raise HTTPException(400, 'Invalid JSON object.')


async def authorize(request, auth, mutation=False):
    session = await asyncio.to_thread(auth.session, request.cookies.get(COOKIE))
    if not session: raise HTTPException(401, 'Please log in.')
    if mutation and not secrets.compare_digest(request.headers.get('x-csrf-token', '').encode(), session['csrf'].encode()):
        raise HTTPException(403, 'Invalid CSRF token.')
    return session


def create_app(origin, auth):
    hub = Hub(auth)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(manager.migrate)
        await asyncio.to_thread(hub.resize.recover)
        try: yield
        finally: await hub.shutdown()

    async def login(request):
        data = await payload(request)
        if not isinstance(data.get('username'), str) or not isinstance(data.get('password'), str) or type(data.get('remember', False)) is not bool:
            raise HTTPException(400, 'Invalid login fields.')
        if len(data['username']) > 100 or len(data['password']) > 1024:
            raise HTTPException(400, 'Login fields are too long.')
        try:
            token, _ = await asyncio.to_thread(auth.login, data['username'], data['password'], data.get('remember', False),
                request.client.host if request.client else 'unknown', request.headers.get('user-agent', 'Browser'))
        except RateLimited:
            return JSONResponse({'error': 'Too many attempts. Try again in one minute.'}, status_code=429, headers={'Retry-After': '60'})
        except LoginDenied: raise HTTPException(401, 'Invalid username or password.')
        response = JSONResponse({'ok': True})
        response.set_cookie(COOKIE, token, max_age=30 * 86400 if data.get('remember') else None,
            secure=True, httponly=True, samesite='strict', path='/')
        return response

    async def current(request):
        session = await authorize(request, auth)
        return JSONResponse({'id': session['id'], 'csrf': session['csrf'], 'expires': session['expires'], 'capabilities': ['pane-scroll']})

    async def logout(request):
        session = await authorize(request, auth, True)
        await asyncio.to_thread(auth.revoke, session['id'])
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict')
        return response

    async def logins(request):
        await authorize(request, auth)
        return JSONResponse(await asyncio.to_thread(auth.list_sessions))

    async def revoke(request):
        await authorize(request, auth, True)
        await asyncio.to_thread(auth.revoke, request.path_params['sid'])
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
        token = ws.cookies.get(COOKIE)
        session = await asyncio.to_thread(auth.session, token)
        if not session: await ws.close(code=4401); return
        run_id = ws.query_params.get('run_id')
        if not run_id: await ws.close(code=4409); return
        try:
            row = await asyncio.to_thread(manager.workspace, ws.path_params['wid'], run_id)
        except (KeyError, click.ClickException): await ws.close(code=4409); return
        await ws.accept()
        client = None
        tasks = []
        try:
            async with hub.lock:
                key = (row['workspace_id'], row['run_id'])
                first = key not in hub.controllers
                if first:
                    # Reconnect follows current selection instead of resetting focus.
                    if ws.query_params.get('reconnect') != '1':
                        windows = await asyncio.to_thread(manager.structure, row)
                        if windows:
                            await asyncio.to_thread(manager.tmux, 'select-window', '-t', row['tmux_id'] + ':' + windows[0]['id'])
                client = await Client.open(ws, hub, row, token)
                if first: hub.controllers[client.key] = client
            async def receive():
                while True:
                    text = await ws.receive_text()
                    if len(text.encode()) > 65536: await ws.close(code=1009); return
                    if not await asyncio.to_thread(auth.session, token): await ws.close(code=4401); return
                    try:
                        message = json.loads(text)
                        if not isinstance(message, dict): raise ValueError('Invalid command.')
                        await client.command(message)
                    except (ValueError, KeyError, click.ClickException) as exc:
                        await client.send({'type': 'error', 'message': str(exc)})
            tasks = [asyncio.create_task(f()) for f in (receive, client.output, client.watch, client.authenticate)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
        except (WebSocketDisconnect, OSError, asyncio.TimeoutError): pass
        except (KeyError, click.ClickException):
            try: await ws.close(code=4409)
            except Exception: pass
        except Exception:
            log.exception('Terminal connection failed')
            try: await ws.close(code=1011)
            except Exception: pass
        finally:
            with anyio.CancelScope(shield=True):
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if client: await client.close()

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
    return Boundary(app, origin)
