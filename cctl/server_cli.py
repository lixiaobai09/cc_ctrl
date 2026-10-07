"""Web command entry points; optional web packages are imported only on use."""
from __future__ import annotations

import fcntl
import json
import os
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import click

TLS_CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20"


def auth(max_login_concurrency=2):
    try:
        from .auth import Auth
    except ImportError as exc:
        raise click.ClickException('Install web dependencies: pip install -e ".[web]"') from exc
    from .cli import CCTL_DIR
    return Auth(CCTL_DIR, max_login_concurrency=max_login_concurrency)


@click.group()
def server():
    """Configure the optional HTTPS server and browser logins."""


@server.command('init')
def initialize():
    """Set the local administrator account interactively."""
    store = auth()
    if store.configured(): raise click.ClickException('Already configured. Use reset-password.')
    username = click.prompt('Administrator username', default='admin')
    password = click.prompt('Password (at least 12 characters)', hide_input=True, confirmation_prompt=True)
    try: store.configure(username, password)
    except ValueError as exc: raise click.ClickException(str(exc)) from exc
    click.echo('Administrator configured. Start cct serve with an HTTPS certificate.')


@server.command('reset-password')
def reset_password():
    """Reset local credentials and revoke every browser login."""
    store = auth()
    username = click.prompt('Administrator username', default='admin')
    password = click.prompt('New password (at least 12 characters)', hide_input=True, confirmation_prompt=True)
    try: store.configure(username, password, reset=True)
    except ValueError as exc: raise click.ClickException(str(exc)) from exc
    click.echo('Password reset; all browser logins revoked.')


@server.group('sessions')
def sessions():
    """List or revoke browser login records."""


@sessions.command('list')
def list_sessions():
    click.echo(json.dumps(auth().list_sessions(), indent=2, ensure_ascii=False))


@sessions.command('revoke')
@click.argument('session_id', required=False)
@click.option('--all', 'all_sessions', is_flag=True)
def revoke(session_id, all_sessions):
    if bool(session_id) == bool(all_sessions):
        raise click.UsageError('Provide a login ID or --all.')
    auth().revoke('--all' if all_sessions else session_id)
    click.echo('Login revoked. Active terminal connections close within five seconds.')


@server.command('audit')
@click.option('--limit', default=50, type=click.IntRange(1, 1000), show_default=True)
def audit_log(limit):
    """Show recent security events, newest first (no credentials or terminal text)."""
    click.echo(json.dumps(auth().audit.read(limit), indent=2, ensure_ascii=False))


@click.command()
@click.option('--host', default='127.0.0.1', show_default=True)
@click.option('--port', default=8443, type=click.IntRange(1, 65535), show_default=True)
@click.option('--cert-file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option('--key-file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option('--origin', required=True, help='Exact browser HTTPS origin, including non-default port.')
@click.option('--login-concurrency', default=2, type=click.IntRange(1, 8), show_default=True, help='Maximum concurrent password verifications; excess logins get HTTP 429.')
def serve(host, port, cert_file, key_file, origin, login_concurrency):
    """Run the authenticated web terminal in the foreground. Ctrl-C stops it."""
    parsed = urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise click.UsageError('--origin must be a single HTTPS origin.')
    origin = origin.rstrip('/')
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.set_ciphers(TLS_CIPHERS)
        context.load_cert_chain(cert_file, key_file)
    except (ssl.SSLError, OSError) as exc:
        raise click.ClickException('Unable to load TLS certificate/key: ' + str(exc)) from exc
    # Keep the same tmux server when invoked inside a custom-socket tmux session.
    if not os.environ.get('CCTL_TMUX_SOCKET') and os.environ.get('TMUX'):
        os.environ['CCTL_TMUX_SOCKET'] = os.environ['TMUX'].rsplit(',', 2)[0]
    store = auth(login_concurrency)
    if not store.configured(): raise click.ClickException('Configure credentials with cct server init first.')
    try:
        import uvicorn
        from .webapp import create_app
    except ImportError as exc:
        raise click.ClickException('Install web dependencies: pip install -e ".[web]"') from exc
    with open(store.root / 'serve.lock', 'a+') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise click.ClickException('A server is already running for this CCTL_HOME.')
        click.echo('Web terminal: ' + origin + ' (Ctrl-C to stop; tmux tasks continue)')
        uvicorn.run(create_app(origin, store), host=host, port=port, workers=1,
            ssl_certfile=str(cert_file), ssl_keyfile=str(key_file), ssl_ciphers=TLS_CIPHERS, proxy_headers=False,
            access_log=False, ws_max_size=65536, ws_max_queue=16, timeout_graceful_shutdown=5)
