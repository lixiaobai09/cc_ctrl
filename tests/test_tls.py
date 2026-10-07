"""Real ECDSA/TLS-1.2 regression: Chrome suites must negotiate with cct serve."""
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

from cctl.auth import Auth
from cctl.server_cli import TLS_CIPHERS


@unittest.skipUnless(shutil.which('openssl'), 'openssl required to generate an ephemeral ECDSA certificate')
class TLSCompatibilityTests(unittest.TestCase):
    def test_ecdsa_server_negotiates_modern_cipher(self):
        with tempfile.TemporaryDirectory(prefix='cct-tls-test-') as directory:
            root=Path(directory); key=root/'key.pem'; cert=root/'cert.pem'
            subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
                '-pkeyopt','ec_param_enc:named_curve','-nodes','-days','1','-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost',
                '-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            auth=Auth(root);auth.configure('admin','tls-regression-password');auth.close()
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
            env=dict(os.environ,CCTL_HOME=str(root),CCTL_TMUX_SOCKET=str(root/'no-server.sock'),
                PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            log=open(root/'service.log','w')
            process=subprocess.Popen([sys.executable,'-m','cctl.cli','serve','--host','127.0.0.1','--port',str(port),
                '--origin',f'https://localhost:{port}','--cert-file',str(cert),'--key-file',str(key)],env=env,stdout=log,stderr=log)
            try:
                for _ in range(60):
                    if process.poll() is not None:self.fail((root/'service.log').read_text())
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                    except OSError:time.sleep(.05)
                else:self.fail('TLS service did not listen')
                client=ssl.create_default_context(cafile=str(cert))
                client.minimum_version=ssl.TLSVersion.TLSv1_2;client.maximum_version=ssl.TLSVersion.TLSv1_2
                client.set_ciphers('ECDHE-ECDSA-AES128-GCM-SHA256')
                with socket.create_connection(('127.0.0.1',port),timeout=3) as raw:
                    with client.wrap_socket(raw,server_hostname='localhost') as conn:
                        self.assertEqual(conn.cipher()[0],'ECDHE-ECDSA-AES128-GCM-SHA256')
                        conn.sendall(f'GET / HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n\r\n'.encode())
                        response=b''
                        while True:
                            chunk=conn.recv(4096)
                            if not chunk:break
                            response+=chunk
                        self.assertTrue(response.startswith(b'HTTP/1.1 200'),response[:100])
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.set_ciphers(TLS_CIPHERS)
                suites=context.get_ciphers()
                self.assertTrue(any(c['name']=='ECDHE-ECDSA-AES128-GCM-SHA256' for c in suites))
                self.assertFalse(any(c['name'].startswith(('ADH-','AECDH-')) or any(x in c['name'] for x in ('NULL','RC4','DES-CBC3')) for c in suites))
            finally:
                process.terminate()
                try:process.wait(timeout=7)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                log.close()
