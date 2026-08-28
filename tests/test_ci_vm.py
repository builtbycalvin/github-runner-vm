import importlib.util
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import signal
import sys
import time
import tempfile
import tarfile
import unittest
import zipfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_vm', ROOT / 'ci_vm.py')
cli = importlib.util.module_from_spec(spec)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ci vm test ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / 'tools'
        self.bin.mkdir()
        fake = self.bin / 'limactl'
        fake.write_text('#!/bin/sh\ncase "$1" in\nlist) echo \'{"name":"ci","status":"Stopped"}\';;\n*) exit 1;;\nesac\n')
        fake.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        self.env.pop('ZDOTDIR', None)
        self.env.pop('BASH_ENV', None)
        self.env.pop('CI_VM_REF', None)

    def install(self, *args):
        return subprocess.run(['bash', str(ROOT / 'install.sh'), '--adopt', 'ci', *args], env=self.env, text=True, capture_output=True, timeout=10)

    def test_install_rerun_shells_and_stopped_readonly(self):
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        config = self.home / '.config/github-runner-vm/config.json'
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(config.read_text())['lima_home'], str(self.home / '.lima'))
        for shell in ('bash', 'zsh'):
            if shutil.which(shell):
                command = 'export PATH="$HOME/.local/bin:$PATH"; ci-vm status'
                result = subprocess.run([shell, '-c', command], env=self.env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Stopped', result.stdout)
        result = subprocess.run([str(self.home / '.local/bin/ci-vm'), 'doctor'], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)

    def test_conflicting_config_preserved(self):
        self.assertEqual(self.install().returncode, 0)
        path = self.home / '.config/github-runner-vm/config.json'
        original = path.read_bytes()
        self.assertNotEqual(self.install('--guest-user', 'other').returncode, 0)
        self.assertEqual(path.read_bytes(), original)

    def test_shell_configuration_is_opt_in_and_idempotent(self):
        original = b'export KEEP_CI_TEST=yes'
        rc = self.home / '.bashrc'
        rc.write_bytes(original)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(rc.read_bytes(), original)
        for _ in range(2):
            result = self.install('--configure-shell')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rc.read_bytes(), original + b'\n' + cli.PATH_BLOCK)
        for shell, options in (('bash', ['--noprofile', '-ic']), ('bash', ['--login', '-ic']), ('zsh', ['-ic'])):
            if not shutil.which(shell):
                continue
            result = subprocess.run([shell, *options, 'ci-vm status'], env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Stopped', result.stdout)
        result = subprocess.run(['bash', '-c', '. "$HOME/.bashrc"; . "$HOME/.bashrc"; printf "%s" "$PATH"'], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.stdout.split(os.pathsep).count(str(self.home / '.local/bin')), 1)

    def test_install_from_extracted_archive_without_git_or_retained_source(self):
        archive = self.home / 'source.zip'
        extracted = self.home / 'extracted source'
        with zipfile.ZipFile(archive, 'w') as package:
            for name in ('install.sh', 'ci_vm.py', 'config/lima.yaml', 'config/provision.sh', 'config/ci-vm-runner.service'):
                package.write(ROOT / name, name)
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)
        self.assertFalse((extracted / '.git').exists())
        for _ in range(2):
            result = subprocess.run(['bash', str(extracted / 'install.sh'), '--adopt', 'ci', '--configure-shell'], env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        shutil.rmtree(extracted)
        result = subprocess.run([str(self.home / '.local/bin/ci-vm'), 'status'], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Stopped', result.stdout)

    def bootstrap_download(self):
        archive = self.home / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as package:
            for name in ('install.sh', 'ci_vm.py', 'config/lima.yaml', 'config/provision.sh', 'config/ci-vm-runner.service'):
                package.add(ROOT / name, arcname='github-runner-vm-main/' + name)
        temporary = self.home / 'download temporary'
        temporary.mkdir()
        self.env.update(TMPDIR=str(temporary), FAKE_ARCHIVE=str(archive), FAKE_DOWNLOAD_CALL=str(self.home / 'download-call'), FAKE_BOOTSTRAP=str(ROOT / 'bootstrap.sh'))
        fake = self.bin / 'curl'
        fake.write_text('#!' + sys.executable + '\n' + '''import json, os, pathlib, shutil, sys
pathlib.Path(os.environ['FAKE_DOWNLOAD_CALL']).write_text(json.dumps(sys.argv[1:]))
if os.environ.get('FAKE_DOWNLOAD_FAIL'):
    sys.exit(22)
if '-o' in sys.argv:
    shutil.copyfile(os.environ['FAKE_ARCHIVE'], sys.argv[sys.argv.index('-o') + 1])
else:
    sys.stdout.write(pathlib.Path(os.environ['FAKE_BOOTSTRAP']).read_text())
''')
        fake.chmod(0o755)
        return archive, temporary

    def run_bootstrap(self, *args, source=None):
        return subprocess.run(['sh', '-s', '--', *args], input=source if source is not None else (ROOT / 'bootstrap.sh').read_text(), cwd=self.home, env=self.env, text=True, capture_output=True, timeout=15)

    def test_piped_bootstrap_installs_reruns_and_pins_source(self):
        _, temporary = self.bootstrap_download()
        for revision in ('main', 'a' * 40):
            self.env['CI_VM_REF'] = revision
            result = self.run_bootstrap('--adopt', 'ci', '--configure-shell')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(temporary.iterdir()), [])
            call = json.loads((self.home / 'download-call').read_text())
            self.assertIn(f'https://github.com/builtbycalvin/github-runner-vm/archive/{revision}.tar.gz', call)
            self.assertEqual(call[call.index('--max-time') + 1], '120')
        result = subprocess.run([str(self.home / '.local/bin/ci-vm'), 'status'], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Stopped', result.stdout)

    def test_bootstrap_download_and_extraction_fail_closed(self):
        archive, temporary = self.bootstrap_download()
        self.env['FAKE_DOWNLOAD_FAIL'] = 'yes'
        result = self.run_bootstrap('--adopt', 'ci')
        self.assertEqual(result.returncode, 22, result.stderr)
        self.assertEqual(list(temporary.iterdir()), [])
        self.assertFalse((self.home / '.local').exists())
        del self.env['FAKE_DOWNLOAD_FAIL']
        archive.write_text('not a tar archive')
        self.assertNotEqual(self.run_bootstrap('--adopt', 'ci').returncode, 0)
        self.assertEqual(list(temporary.iterdir()), [])
        self.assertFalse((self.home / '.local').exists())

    def test_documented_pipeline_fetch_failure_and_success_in_both_shells(self):
        _, temporary = self.bootstrap_download()
        command = next(block.split('\n', 1)[1] for block in (ROOT / 'README.md').read_text().split('```')[1::2]
                       if '| sh -s -- --adopt ci --configure-shell' in block)
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            self.env['FAKE_DOWNLOAD_FAIL'] = 'yes'
            result = subprocess.run([shell, '-c', command], env=self.env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 22, result.stderr)
            self.assertEqual(list(temporary.iterdir()), [])
        self.assertFalse((self.home / '.local').exists())
        del self.env['FAKE_DOWNLOAD_FAIL']
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            result = subprocess.run([shell, '-c', command], env=self.env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Installed ci-vm.', result.stdout)
            self.assertEqual(list(temporary.iterdir()), [])

    def test_bootstrap_requires_arguments_valid_revision_and_complete_script(self):
        _, temporary = self.bootstrap_download()
        self.assertEqual(self.run_bootstrap().returncode, 2)
        for revision in ('../other', 'v1', 'short', 'z' * 40):
            self.env['CI_VM_REF'] = revision
            self.assertEqual(self.run_bootstrap('--adopt', 'ci').returncode, 2)
        self.env.pop('CI_VM_REF')
        partial = (ROOT / 'bootstrap.sh').read_text().rsplit('\nbootstrap "$@"', 1)[0]
        self.assertEqual(self.run_bootstrap('--adopt', 'ci', source=partial).returncode, 0)
        self.assertFalse((self.home / 'download-call').exists())
        self.assertEqual(list(temporary.iterdir()), [])
        self.assertFalse((self.home / '.local').exists())

    def test_shell_configuration_preserves_login_precedence_and_zdotdir(self):
        (self.home / '.profile').write_text('export KEEP_CI_TEST=yes\n')
        zdot = self.home / 'zsh config'
        self.env['ZDOTDIR'] = str(zdot)
        result = self.install('--configure-shell')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / '.bash_profile').exists())
        self.assertTrue((self.home / '.profile').read_bytes().endswith(cli.PATH_BLOCK))
        self.assertEqual((zdot / '.zshrc').read_bytes(), cli.PATH_BLOCK)
        self.assertFalse((self.home / '.zshrc').exists())

    def test_shell_configuration_refuses_unsafe_or_modified_files(self):
        target = self.home / 'keep'
        target.write_text('keep')
        rc = self.home / '.zshrc'
        rc.symlink_to(target)
        self.assertNotEqual(self.install('--configure-shell').returncode, 0)
        self.assertEqual(target.read_text(), 'keep')
        self.assertFalse((self.home / '.bashrc').exists())
        rc.unlink()
        rc.write_bytes(cli.PATH_BLOCK.replace(b'export PATH=', b'PATH='))
        self.assertNotEqual(self.install('--configure-shell').returncode, 0)
        self.assertFalse((self.home / '.bashrc').exists())
        rc.unlink()
        for directory in ('/tmp', str(self.home / '..' / 'outside'), 'relative'):
            self.env['ZDOTDIR'] = directory
            self.assertNotEqual(self.install('--configure-shell').returncode, 0)
            self.assertFalse((self.home / '.bashrc').exists())

    def test_unrelated_launcher_preserved(self):
        path = self.home / '.local/bin/ci-vm'
        path.parent.mkdir(parents=True)
        path.write_text('do not change')
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(path.read_text(), 'do not change')

    def test_unrelated_share_and_missing_vm_preserved(self):
        share = self.home / '.local/share/github-runner-vm'
        share.mkdir(parents=True)
        (share / 'keep').write_text('private')
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual((share / 'keep').read_text(), 'private')

    def test_partial_owned_install_converges(self):
        share = self.home / '.local/share/github-runner-vm'
        config = self.home / '.config/github-runner-vm'
        for directory in (share, config):
            directory.mkdir(parents=True)
            (directory / '.github-runner-vm-owner').write_text('github-runner-vm installation v1\n')
        self.assertEqual(self.install().returncode, 0)

    def test_runtime_config_symlink_refused(self):
        self.assertEqual(self.install().returncode, 0)
        path = self.home / '.config/github-runner-vm/config.json'
        target = self.home / 'secret.json'
        path.rename(target)
        path.symlink_to(target)
        result = subprocess.run([str(self.home / '.local/bin/ci-vm'), 'status'], env=self.env, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(target.exists())

    def test_owned_launcher_updates_after_python_changes(self):
        self.assertEqual(self.install().returncode, 0)
        path = self.home / '.local/bin/ci-vm'
        module = self.home / '.local/share/github-runner-vm/ci_vm.py'
        path.write_text(cli.launcher_text('/old/python3', module))
        self.assertEqual(self.install().returncode, 0)
        self.assertNotIn('/old/python3', path.read_text())
        modified = path.read_text() + 'echo changed\n'
        path.write_text(modified)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(path.read_text(), modified)

    def test_install_and_maintenance_share_nonmutating_lock(self):
        descriptor = os.open(self.home, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.install()
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse((self.home / '.local').exists())
        finally:
            os.close(descriptor)
        self.assertEqual(self.install().returncode, 0)
        descriptor = os.open(self.home, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([str(self.home / '.local/bin/ci-vm'), 'pause'], env=self.env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 3)
        finally:
            os.close(descriptor)

    def test_unsafe_home_and_ancestor_refused(self):
        self.home.chmod(0o777)
        self.assertNotEqual(self.install().returncode, 0)
        self.home.chmod(0o700)
        directory = self.home / '.local'
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(list(directory.iterdir()), [])

    def test_home_symlink_refused(self):
        link = self.home / 'linked-home'
        link.symlink_to(self.home, target_is_directory=True)
        self.env['HOME'] = str(link)
        self.assertNotEqual(self.install().returncode, 0)

    def test_duplicate_config_keys_refused(self):
        self.assertEqual(self.install().returncode, 0)
        config = self.home / '.config/github-runner-vm/config.json'
        config.write_text(config.read_text().replace('"version": 1,', '"version": 1, "version": 1,'))
        result = subprocess.run([str(self.home / '.local/bin/ci-vm'), 'status'], env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)

    def fake_running_tools(self):
        fake = self.bin / 'limactl'
        fake.write_text('#!' + sys.executable + "\n" + r'''import hashlib, json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FAKE_CALLS'], 'a') as calls:
    calls.write(json.dumps(args) + '\n')
if args[0] == 'list':
    print(json.dumps(dict(name='ci', status='Running')))
elif args[0] == 'shell':
    script = sys.stdin.read()
    if 'UnitHash' in script:
        unit = pathlib.Path.home() / '.local/share/github-runner-vm/config/ci-vm-runner.service'
        print('LoadState=loaded\nFragmentPath=/etc/systemd/user/ci-vm-runner.service\nDropInPaths=\nNeedDaemonReload=no\nTransient=no')
        print('UnitHash=' + hashlib.sha256(unit.read_bytes()).hexdigest())
        print('UnitOwner=0:644\nActualUID=1001\nMarkerOwner=0:755')
    elif 'touch -- /var/lib/ci-vm/paused' in script:
        pathlib.Path(os.environ['FAKE_PAUSED']).touch()
    elif 'CgroupEmpty' in script:
        phase = os.environ.get('FAKE_PHASE', 'paused')
        print('ActiveState=' + ('inactive' if phase == 'paused' else 'active'))
        print('SubState=' + ('dead' if phase == 'paused' else 'running'))
        print('MainPID=0\nControlPID=0\nControlGroup=\nJob=0\nPaused=yes\nRunners=0\nContainers=0\nJobs=0\nCgroupEmpty=yes')
    else:
        print('LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=123')
else:
    sys.exit(2)
''')
        self.env.update(FAKE_CALLS=str(self.home / 'calls'), FAKE_PAUSED=str(self.home / 'paused'))

    def test_full_pause_and_pending_with_fake_limactl(self):
        self.assertEqual(self.install().returncode, 0)
        self.fake_running_tools()
        launcher = self.home / '.local/bin/ci-vm'
        result = subprocess.run([str(launcher), 'pause'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / 'paused').exists())
        self.env['FAKE_PHASE'] = 'running'
        result = subprocess.run([str(launcher), 'pause', '--timeout', '0.3'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 4, result.stderr)
        calls = [json.loads(line) for line in (self.home / 'calls').read_text().splitlines()]
        self.assertTrue(all(call[0] in {'list', 'shell'} for call in calls))

    def test_fake_github_exact_readback_and_mismatch(self):
        self.assertEqual(self.install('--repo', 'owner/repo', '--runner-id', '7').returncode, 0)
        self.fake_running_tools()
        fake = self.bin / 'gh'
        fake.write_text('#!' + sys.executable + "\n" + r'''import json, os, sys
if sys.argv[1:] != ['api', '--hostname', 'github.com', 'repos/owner/repo/actions/runners/7']:
    sys.exit(2)
print(json.dumps(dict(id=int(os.environ.get('FAKE_ID', '7')), busy=False, status='online')))
''')
        fake.chmod(0o755)
        launcher = self.home / '.local/bin/ci-vm'
        result = subprocess.run([str(launcher), 'status'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GitHub runner 7: online', result.stdout)
        self.env['FAKE_ID'] = '8'
        result = subprocess.run([str(launcher), 'status'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 3)

    def test_symlink_refused(self):
        path = self.home / '.local'
        target = self.home / 'elsewhere'
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_missing_vm_does_not_install_or_provision(self):
        (self.bin / 'limactl').write_text('#!/bin/sh\nexit 0\n')
        result = self.install()
        self.assertEqual(result.returncode, 2)
        self.assertIn('Adoption never provisions', result.stderr)
        self.assertFalse((self.home / '.local').exists())

    def test_provision_requires_consent_and_refuses_existing_vm(self):
        command = ['bash', str(ROOT / 'install.sh'), '--provision', 'ci']
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--yes-create-vm', result.stderr)
        result = subprocess.run(command + ['--yes-create-vm'], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / '.local').exists())


class CommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec.loader.exec_module(cli)

    def setUp(self):
        self.config = dict(version=1, vm='ci', lima_home='/tmp/lima', guest_user='ci', guest_uid=1001, unit='ci-vm-runner.service')

    def test_config_rejects_injection_and_unpaired_identity(self):
        for update in ({'vm': 'ci;true'}, {'vm': '.'}, {'vm': '..'}, {'unit': '../bad'}, {'repo': 'owner/repo'}, {'guest_uid': True}, {'lima_home': 'relative'}):
            with self.subTest(update=update), self.assertRaises(cli.Failure):
                cli.validate_config(dict(self.config, **update))

    def test_timeout_is_bounded(self):
        with self.assertRaises(cli.Failure) as result:
            cli.run(['sh', '-c', 'sleep 10'], cli.deadline(0.05))
        self.assertEqual(result.exception.code, 4)

    def test_timeout_permission_denial_still_cleans_up_and_reports_uncertainty(self):
        for wait_error in (None, subprocess.TimeoutExpired('tool', 1)):
            with self.subTest(child_still_running=wait_error is not None):
                process = Mock(pid=123)
                process.communicate.side_effect = subprocess.TimeoutExpired('tool', 0.1)
                process.wait.side_effect = wait_error
                with patch.object(cli.subprocess, 'Popen', return_value=process), patch.object(cli.os, 'killpg', side_effect=PermissionError(1, 'Operation not permitted')):
                    with self.assertRaises(cli.Failure) as result:
                        cli.run(['tool'], cli.deadline(0.1), input='probe')
                self.assertEqual(result.exception.code, 4)
                self.assertIn('Host process-group cleanup was denied', str(result.exception))
                self.assertIn('may still be running', str(result.exception))
                process.wait.assert_called_once_with(timeout=1)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close.assert_called_once_with()

    def test_missing_executable_has_actionable_error(self):
        with self.assertRaises(cli.Failure) as result:
            cli.run(['/nonexistent-ci-vm-test-tool'], cli.deadline(1))
        self.assertIn('Cannot run', str(result.exception))

    def test_unknown_vm_output_refuses(self):
        with patch.object(cli, 'lima', return_value='garbage'), self.assertRaises(cli.Failure):
            cli.vm_state(self.config, cli.deadline(1))

    def test_pause_pending_never_stops(self):
        calls = []
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'verify_contract'), patch.object(cli, 'guest', side_effect=lambda *a, **k: calls.append(a) or ''), patch.object(cli, 'idle', return_value=False):
            with self.assertRaises(cli.Failure) as result:
                cli.maintain(self.config, 'pause', cli.deadline(0.03))
        self.assertEqual(result.exception.code, 4)
        self.assertFalse(any('stop' in str(call) or 'kill' in str(call) for call in calls))

    def test_incompatible_refuses_before_mutation(self):
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'verify_contract', side_effect=cli.Failure('incompatible', 3)), patch.object(cli, 'guest') as guest:
            with self.assertRaises(cli.Failure):
                cli.maintain(self.config, 'pause', cli.deadline(1))
            guest.assert_not_called()

    def test_restart_requires_paused_idle(self):
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'verify_contract'), patch.object(cli, 'idle', return_value=False), patch.object(cli, 'lima') as lima:
            with self.assertRaises(cli.Failure):
                cli.maintain(self.config, 'restart', cli.deadline(1))
            lima.assert_not_called()

    def idle_output(self, **changes):
        state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', ControlGroup='', Job='0', Paused='yes', Runners='0', Containers='0', Jobs='0', CgroupEmpty='yes')
        state.update(changes)
        return ''.join(f'{key}={value}\n' for key, value in state.items())

    def test_each_restart_guard_is_required(self):
        for change in ({'ActiveState': 'activating'}, {'SubState': 'auto-restart'}, {'MainPID': '5'}, {'ControlPID': '5'}, {'Job': '7'}, {'Paused': 'no'}, {'Runners': '1'}, {'Containers': '1'}, {'Jobs': '1'}, {'CgroupEmpty': 'no'}):
            with self.subTest(change=change), patch.object(cli, 'guest', return_value=self.idle_output(**change)):
                self.assertFalse(cli.idle(self.config, cli.deadline(1)))
        with patch.object(cli, 'guest', return_value=self.idle_output()):
            self.assertTrue(cli.idle(self.config, cli.deadline(1)))

    def test_idle_missing_output_refuses(self):
        for output in ('', 'ActiveState=inactive\n', self.idle_output(Runners='unknown'), self.idle_output() + 'Job=0\n'):
            with self.subTest(output=output), patch.object(cli, 'guest', return_value=output), self.assertRaises(cli.Failure):
                cli.idle(self.config, cli.deadline(1))

    def test_cgroup_probe_reads_virtual_files(self):
        with patch.object(cli, 'guest', return_value=self.idle_output()) as guest:
            cli.idle(self.config, cli.deadline(1))
        script = guest.call_args.args[1]
        self.assertNotIn('[ -s "$file" ]', script)
        self.assertIn('cat "$file"', script)
        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stopped_resume_never_starts_vm(self):
        events = []
        with patch.object(cli, 'vm_state', side_effect=['Stopped', 'Running']), patch.object(cli, 'lima', side_effect=lambda *a: events.append('start') or ''), patch.object(cli, 'verify_contract', side_effect=cli.Failure('mismatch', 3)), patch.object(cli, 'guest') as guest:
            with self.assertRaises(cli.Failure):
                cli.maintain(self.config, 'resume', cli.deadline(1))
            guest.assert_not_called()
        self.assertEqual(events, [])

    def test_restart_preserves_pause(self):
        commands = []
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'verify_contract'), patch.object(cli, 'idle', return_value=True), patch.object(cli, 'lima', side_effect=lambda config, args, until: commands.append(args) or ''), patch.object(cli, 'guest') as guest:
            cli.maintain(self.config, 'restart', cli.deadline(1))
            guest.assert_not_called()
        self.assertEqual(commands, [['stop', 'ci'], ['start', '--tty=false', 'ci']])

    def test_timeout_does_not_wait_for_detached_pipe_holder(self):
        with tempfile.TemporaryDirectory() as temporary:
            pidfile = Path(temporary) / 'pid'
            program = 'import pathlib, subprocess, sys, time; child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True); pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(20)'
            start = time.monotonic()
            try:
                with self.assertRaises(cli.Failure) as result:
                    cli.run([sys.executable, '-c', program, str(pidfile)], cli.deadline(0.25))
                self.assertEqual(result.exception.code, 4)
                self.assertLess(time.monotonic() - start, 1.5)
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_failure_reports_only_allowlisted_diagnostics(self):
        program = 'import sys; print("FAIL: Docker is not rootless"); print("token=private"); print("secret", file=sys.stderr); sys.exit(1)'
        with self.assertRaises(cli.Failure) as result:
            cli.run([sys.executable, '-c', program], cli.deadline(2))
        self.assertIn('Docker is not rootless', str(result.exception))
        self.assertNotIn('private', str(result.exception))
        self.assertNotIn('secret', str(result.exception))

    def test_restart_refuses_inherited_config_and_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dict(self.config, lima_home=temporary)
            directory = Path(temporary) / '_config'
            directory.mkdir()
            for name in ('default.yaml', 'override.yaml', 'base.yaml'):
                path = directory / name
                path.symlink_to(directory / 'missing')
                with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'verify_contract'), patch.object(cli, 'lima') as lima:
                    with self.assertRaises(cli.Failure):
                        cli.maintain(config, 'restart', cli.deadline(1))
                    lima.assert_not_called()
                path.unlink()

    def test_alias_service_is_readonly(self):
        with patch.object(cli, 'guest') as guest, self.assertRaises(cli.Failure):
            cli.verify_contract(dict(self.config, unit='alias.service'), cli.deadline(1))
        guest.assert_not_called()

    def test_stopped_pause_refuses(self):
        with patch.object(cli, 'vm_state', return_value='Stopped'), patch.object(cli, 'guest') as guest:
            with self.assertRaises(cli.Failure):
                cli.maintain(self.config, 'pause', cli.deadline(1))
            guest.assert_not_called()


if __name__ == '__main__':
    unittest.main()
