import json
import subprocess
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cctl import cli


class WorkspaceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, value in {
            'CCTL_DIR': self.root,
            'STORE_FILE': self.root / 'workspaces.json',
            'HISTORY_FILE': self.root / 'history.json',
            'DEFAULT_CMD': 'codex',
        }.items():
            self.stack.enter_context(patch.object(cli, key, value))
        for name in ('_require_tmux', '_warn_if_codex_hook_missing', '_switch_to'):
            self.stack.enter_context(patch.object(cli, name))
        self.stack.enter_context(patch.object(cli, '_session_exists', return_value=False))
        self.stack.enter_context(patch.object(cli, '_live_sessions', return_value={'a', 'b'}))
        self.tmux = self.stack.enter_context(patch.object(
            cli, '_tmux', return_value=subprocess.CompletedProcess([], 0, '', '')
        ))
        self.runner = CliRunner()

    def invoke(self, args):
        result = self.runner.invoke(cli.main, args)
        self.assertEqual(result.exit_code, 0, result.output)

    def test_same_directory_workspaces_launch_isolated_and_capture_separate_ids(self):
        ids = {}
        for name in ('a', 'b'):
            self.invoke(['create', name, '--cwd', str(self.root)])
            self.tmux.assert_any_call('send-keys', '-t', name, 'codex --no-daemon', 'Enter')
            ids[name] = str(uuid.uuid4())
            self.assertTrue(cli._capture_codex_session({
                'hook_event_name': 'SessionStart', 'session_id': ids[name],
                'cwd': str(self.root), 'transcript_path': '/transcript.jsonl',
            }, name))
        history = json.loads(cli.HISTORY_FILE.read_text())
        self.assertEqual({name: history[name]['session_id'] for name in ids}, ids)

    def test_restore_uses_isolated_process(self):
        sid = str(uuid.uuid4())
        cli.HISTORY_FILE.write_text(json.dumps({'old': {
            'cwd': str(self.root), 'engine': 'codex', 'session_id': sid,
        }}))
        self.invoke(['restore', 'old', '--as', 'a'])
        self.tmux.assert_any_call('send-keys', '-t', 'a', f'codex --no-daemon resume {sid}', 'Enter')

    def test_override_preserves_prompt_and_does_not_duplicate_flag(self):
        cmd = 'codex --no-daemon -- "a quoted prompt"'
        self.invoke(['create', 'a', '--cmd', cmd])
        self.tmux.assert_any_call('send-keys', '-t', 'a', cmd, 'Enter')
        self.assertEqual(cli._isolate_codex_command('codex -- "--no-daemon"'),
                         'codex --no-daemon -- "--no-daemon"')

    def test_remote_override_rejected_before_creating_session(self):
        result = self.runner.invoke(cli.main, ['create', 'a', '--cmd', 'codex --remote unix://'])
        self.assertNotEqual(result.exit_code, 0)
        self.tmux.assert_not_called()
        self.assertFalse(cli.HISTORY_FILE.exists())

    def test_hook_uses_current_python_instead_of_other_installation_on_path(self):
        with patch.object(cli.shutil, 'which', return_value='/old/cctl'):
            command = cli._codex_capture_handler()['command']
        self.assertIn('-m cctl.cli codex-hook capture', command)
        self.assertNotIn('/old/cctl', command)

    def test_other_engines_unchanged(self):
        for cmd in ('claude --resume abc', 'qodercli', '', 'bash'):
            self.assertEqual(cli._isolate_codex_command(cmd), cmd)


if __name__ == '__main__':
    unittest.main()
