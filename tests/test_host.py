"""Running the harness on a box without root: where the shim is. No agent, no network."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_impute_broker import REPO, config
from harness import agents, auth, manifest, probe, profile


def bwrap_usable():
    """Can this account start a bubblewrap namespace at all (root, or unprivileged userns)?"""
    bwrap = shutil.which('bwrap')
    if not bwrap:
        return False
    return subprocess.run([bwrap, '--ro-bind', '/', '/', '--unshare-user', '--', '/bin/true'],
                          capture_output=True).returncode == 0


class SandboxExecTest(unittest.TestCase):
    def test_resolution_order(self):
        with patch.dict(os.environ, {config.SANDBOX_EXEC_ENV: '/opt/shim/sandbox-exec'}):
            self.assertEqual(config._sandbox_exec(), '/opt/shim/sandbox-exec')
        env = {k: v for k, v in os.environ.items() if k != config.SANDBOX_EXEC_ENV}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(config.os, 'access', return_value=True):
                self.assertEqual(config._sandbox_exec(), '/usr/bin/sandbox-exec')
            with patch.object(config.os, 'access', return_value=False):
                self.assertEqual(config._sandbox_exec(), str(REPO / 'harness' / 'sandbox-exec'))

    def test_the_repository_shim_is_the_installed_one(self):
        self.assertTrue(os.access(config.SANDBOX_EXEC_REPO, os.X_OK))
        if os.path.exists(config.SANDBOX_EXEC_INSTALLED):
            self.assertEqual(Path(config.SANDBOX_EXEC_INSTALLED).read_bytes(),
                             Path(config.SANDBOX_EXEC_REPO).read_bytes())

    def test_every_launch_goes_through_the_configured_shim(self):
        with patch.object(config, 'SANDBOX_EXEC', '/opt/shim/sandbox-exec'):
            agent = agents.SandboxedClaude('prs', '/s', '/s/profile.sb', '/s/.home', {})
            self.assertEqual(agent._argv()[:3], ['/opt/shim/sandbox-exec', '-f', '/s/profile.sb'])
            with patch.object(probe.subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, '{"reads": [], "writes": []}', '')
                probe.run_denial_probe('/s/profile.sb', '/s', '/s/.home', {})
            self.assertEqual(run.call_args[0][0][:3], ['/opt/shim/sandbox-exec', '-f', '/s/profile.sb'])
            self.assertEqual(manifest.host_facts()['sandbox_exec_path'], '/opt/shim/sandbox-exec')
            self.assertIn('read by /opt/shim/sandbox-exec',
                          profile.build_profile('/s', '/s/.home', None, allow_keychain=False))

    def test_the_prompt_and_system_prompt_never_ride_on_argv(self):
        # argv is world-readable through /proc on a shared host (other agents run outside the
        # sandbox), so neither the prompt nor an appended system prompt may ride on it. The prompt
        # goes on stdin in print mode; an appended system prompt goes in a 0600 file named by
        # --append-system-prompt-file.
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 's')
            os.makedirs(root)
            agent = agents.SandboxedClaude('prs', root, os.path.join(root, 'p.sb'),
                                           os.path.join(root, '.home'), {},
                                           append_system_prompt='SECRET-SYSTEM-PROMPT')
            seen = {}

            class FakePopen:
                def __init__(self, argv, **kw):
                    seen['argv'] = argv
                    seen['stdin'] = kw.get('stdin')

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def communicate(self, input=None, timeout=None):
                    seen['input'] = input
                    seen['spf_exists_during_run'] = any(
                        a == '--append-system-prompt-file' for a in seen['argv'])
                    return ('out', '')

                def poll(self):
                    return 0

                def wait(self, timeout=None):
                    return 0

            with patch.object(agents.subprocess, 'Popen', FakePopen):
                agent.run('MY-SECRET-PROMPT-TEXT', timeout=30)
            argv = seen['argv']
            self.assertIn('-p', argv)
            self.assertNotIn('MY-SECRET-PROMPT-TEXT', ' '.join(argv))  # prompt not on argv
            self.assertNotIn('SECRET-SYSTEM-PROMPT', ' '.join(argv))   # nor the system prompt
            self.assertEqual(seen['input'], 'MY-SECRET-PROMPT-TEXT')   # prompt delivered on stdin
            self.assertEqual(seen['stdin'], subprocess.PIPE)
            i = argv.index('--append-system-prompt-file')
            spf = argv[i + 1]
            # the appended system prompt rides in a private file, named on argv, gone after the run
            self.assertEqual(os.path.dirname(spf), root)
            self.assertFalse(os.path.exists(spf))  # cleaned up when the run ended
            # a run with no appended system prompt names no such file
            plain = agents.SandboxedClaude('prs', root, 'p.sb', '.home', {})
            self.assertNotIn('--append-system-prompt-file', plain._argv())
            self.assertIn('-p', plain._argv())

    @unittest.skipUnless(bwrap_usable(), 'needs a usable bubblewrap')
    def test_the_repository_shim_runs_a_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'sandbox')
            os.makedirs(os.path.join(root, 'refdata'))
            path = profile.write_profile(
                os.path.join(tmp, 'profile.sb'),
                profile.build_profile(root, None, None, allow_keychain=False))
            proc = subprocess.run([config.SANDBOX_EXEC_REPO, '-f', path, '/usr/bin/python3', '-I',
                                   '-c', 'import os; print(os.getuid(), os.getcwd())'],
                                  capture_output=True, text=True, timeout=60,
                                  env={'PATH': config.SANDBOX_PATH})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.split(), [str(profile.SANDBOX_UID), root])


class TokenFileTest(unittest.TestCase):
    """The Claude token file: named, else private config, else /root/.env; the mode check stays."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='token-test-')
        self.addCleanup(self.tmp.cleanup)
        self.private = os.path.join(self.tmp.name, 'claude.env')
        patcher = patch.object(auth, 'TOKEN_FILE_PRIVATE', self.private)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, mode=0o600, text='CLAUDE_CODE_OAUTH_TOKEN=synthetic-test-value\n'):
        with open(path, 'w') as fh:
            fh.write(text)
        os.chmod(path, mode)

    def test_resolution_order(self):
        self.assertEqual(auth.token_file({}), '/root/.env')
        self.write(self.private)
        self.assertEqual(auth.token_file({}), self.private)
        self.assertEqual(auth.token_file({auth.TOKEN_FILE_ENV: '/elsewhere/claude.env'}),
                         '/elsewhere/claude.env')

    def test_the_private_file_supplies_the_token(self):
        self.write(self.private)
        environ = {}
        source, line = auth.load_token(environ=environ, platform='linux')
        self.assertEqual((source, environ[auth.TOKEN_VAR]), ('file', 'synthetic-test-value'))
        self.assertIn(self.private, line)
        self.assertNotIn('synthetic-test-value', line)

    def test_a_named_file_that_others_can_read_is_refused(self):
        named = os.path.join(self.tmp.name, 'named.env')
        self.write(named, mode=0o644)
        with self.assertRaisesRegex(auth.MissingToken, 'readable by more than its owner'):
            auth.load_token(environ={auth.TOKEN_FILE_ENV: named}, platform='linux')

    def test_the_private_configuration_is_on_the_denial_list(self):
        paths = [p for p, _ in config.deny_probe_paths('/jail')]
        self.assertIn(os.path.dirname(config.PRIVATE_TOKENS_DEFAULT), paths)

    @unittest.skipUnless(bwrap_usable(), 'needs a usable bubblewrap')
    def test_the_private_configuration_is_unreachable_from_a_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'sandbox')
            os.makedirs(os.path.join(root, 'refdata'))
            jail = os.path.join(root, '.home')
            os.makedirs(jail)
            path = profile.write_profile(
                os.path.join(tmp, 'profile.sb'),
                profile.build_profile(root, jail, None, allow_keychain=False))
            env = {'PATH': config.SANDBOX_PATH, 'HOME': jail, 'TMPDIR': root + '/'}
            reads = {r['path']: r['result'] for r in
                     probe.run_denial_probe(path, root, jail, env)['reads']}
            self.assertIn(reads[os.path.dirname(config.PRIVATE_TOKENS_DEFAULT)],
                          ('absent', 'denied'))


class HostConfigTest(unittest.TestCase):
    """host.json: the toolchain prefixes and the CLI a box keeps outside the system trees."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='host-test-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.tools = self.dir / 'tools'
        (self.tools / 'env' / 'bin').mkdir(parents=True)
        tool = self.tools / 'env' / 'bin' / 'fake-tool'
        tool.write_text('#!/bin/sh\necho tool-ran\n')
        tool.chmod(0o755)
        self.cli = self.tools / 'claude-test'
        self.cli.write_text('#!/bin/sh\necho cli-ran\n')
        self.cli.chmod(0o755)
        (self.tools / 'secret.txt').write_text('not for the sandbox')
        self.sandboxes = self.dir / 'harness' / 'sandboxes'
        self.sandboxes.mkdir(parents=True)
        patcher = patch.object(config, 'SANDBOXES', str(self.sandboxes))
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, data):
        path = self.dir / 'host.json'
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return config.load_host_config(str(path))

    def test_no_file_changes_nothing(self):
        self.assertEqual(config.load_host_config(str(self.dir / 'absent.json')),
                         {'path': None, 'tool_prefixes': [], 'claude_cli': None})

    def test_a_valid_file(self):
        host = self.load({'tool_prefixes': [str(self.tools / 'env')], 'claude_cli': str(self.cli)})
        self.assertEqual(host['tool_prefixes'], [str(self.tools / 'env')])
        self.assertEqual(host['claude_cli'], str(self.cli))

    def test_a_bad_file_refuses(self):
        for data in ('{not json', ['a list'], {'tool_prefixes': ['relative/env']},
                     {'tool_prefixes': [str(self.dir / 'missing')]},
                     {'claude_cli': str(self.tools / 'secret.txt')},
                     {'claude_cli': str(self.cli), 'extra_key': 1}):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                self.load(data)

    def test_a_parent_that_holds_the_sandboxes_refuses(self):
        prefix = self.dir / 'harness' / 'env'
        prefix.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'would cover them'):
            self.load({'tool_prefixes': [str(prefix)]})

    def test_who_gets_what(self):
        host = {'path': 'x', 'tool_prefixes': ['/t/env', '/t/bio'], 'claude_cli': '/t/claude'}
        with patch.object(config, 'HOST', host):
            self.assertEqual(config.host_reads('prs'), ['/t/env', '/t/bio', '/t/claude'])
            self.assertEqual(config.host_reads('provider'), ['/t/env', '/t/bio', '/t/claude'])
            self.assertEqual(config.host_reads('persona'), ['/t/claude'])
        with patch.object(config, 'SANDBOX_PATH_HOST', '/t/env/bin:' + config.SANDBOX_PATH):
            self.assertEqual(agents.build_env('/j', '/j/tmp', None)['PATH'],
                             '/t/env/bin:' + config.SANDBOX_PATH)
        with patch.object(config, 'CLAUDE_CLI', '/t/claude'):
            self.assertEqual(agents.SandboxedClaude('prs', '/s', '/p', '/j', {})._argv()[3],
                             '/t/claude')

    def test_the_account_home_is_probed_when_home_is_redirected(self):
        real = pwd_entry(str(self.dir / 'account'))
        with patch.object(config.pwd, 'getpwuid', return_value=real):
            probes = dict(config.deny_probe_paths('/jail'))
        for name in ('.claude', '.claude.json', '.ssh', '.config', '.bash_history'):
            self.assertIn(str(self.dir / 'account' / name), probes)
        with patch.object(config.pwd, 'getpwuid', return_value=pwd_entry(os.path.expanduser('~'))):
            self.assertEqual(config._account_home_probes(), [])

    @unittest.skipUnless(bwrap_usable(), 'needs a usable bubblewrap')
    def test_inside_the_sandbox(self):
        host = self.load({'tool_prefixes': [str(self.tools / 'env')], 'claude_cli': str(self.cli)})
        root = self.sandboxes / 'run' / 'prs'
        (root / 'refdata').mkdir(parents=True)
        with patch.object(config, 'HOST', host):
            reads = config.host_reads('prs')
        path = profile.write_profile(str(self.dir / 'profile.sb'), profile.build_profile(
            str(root), None, None, allow_keychain=False, extra_read=reads))
        check = ('import os, subprocess, sys\n'
                 'tools = sys.argv[1]\n'
                 'def write(p):\n'
                 '    try:\n'
                 '        open(p, "w").write("x"); return "written"\n'
                 '    except OSError as e: return e.strerror\n'
                 'print(subprocess.run(["fake-tool"], capture_output=True, text=True).stdout.strip())\n'
                 'print(subprocess.run([sys.argv[2]], capture_output=True, text=True).stdout.strip())\n'
                 'print(sorted(os.listdir(tools)))\n'
                 'print(write(os.path.join(tools, "env", "bin", "x")))\n'
                 'print(write("own-file"))\n')
        proc = subprocess.run([config.SANDBOX_EXEC, '-f', path, '/usr/bin/python3', '-I', '-c',
                               check, str(self.tools), str(self.cli)],
                              cwd=str(root), capture_output=True, text=True, timeout=60,
                              env={'PATH': str(self.tools / 'env' / 'bin') + ':' + config.SANDBOX_PATH})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),
                         ['tool-ran', 'cli-ran', "['claude-test', 'env']",
                          'Read-only file system', 'written'])


class FreshCloneTest(unittest.TestCase):
    @unittest.skipUnless(bwrap_usable(), 'needs a usable bubblewrap')
    def test_probe_makes_the_sandboxes_directory(self):
        import run_harness
        with tempfile.TemporaryDirectory() as tmp:
            sandboxes = os.path.join(tmp, 'not-yet', 'sandboxes')
            with patch.object(config, 'SANDBOXES', sandboxes), \
                    patch('sys.stdout', new_callable=lambda: open(os.devnull, 'w')):
                self.assertEqual(run_harness.cmd_probe(None), 0)
            self.assertTrue(os.path.isdir(sandboxes))


def pwd_entry(home):
    import pwd
    real = pwd.getpwuid(os.getuid())
    return pwd.struct_passwd((real.pw_name, real.pw_passwd, real.pw_uid, real.pw_gid,
                              real.pw_gecos, home, real.pw_shell))


if __name__ == '__main__':
    unittest.main()
