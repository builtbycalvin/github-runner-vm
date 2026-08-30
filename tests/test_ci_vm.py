import importlib.util
import io
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
ARCHIVE_FILES = (
    'install.sh', 'ci_vm.py', 'ci_vm_checks.py', 'config/lima.yaml', 'config/provision.sh', 'config/ci-vm-runner.service', 'config/ci-vm-runner@.service', 'config/prepare-shared-runner.sh', 'config/container-runtime-state.sh',
    'docs/setup.md', 'docs/maintenance.md', 'docs/security.md', 'docs/llm-setup.md', 'examples/smoke.yml',
)
spec = importlib.util.spec_from_file_location('ci_vm', ROOT / 'ci_vm.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
SUPPORTED_DARWIN_ARM64 = cli.HostArchitecture('Darwin', 'arm64', 'arm64', False)


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
        (self.home / '.bash_profile').write_text('export PATH="$HOME/tools:$PATH"\n')
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(rc.read_bytes(), original)
        for _ in range(2):
            result = self.install('--configure-shell')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rc.read_bytes(), original + b'\n' + cli.PATH_BLOCK)
        for shell, options in (('bash', ['--noprofile', '-ic']), ('bash', ['--login', '-ic']), ('zsh', ['-ic'])):
            if not shutil.which(shell):
                continue
            command = 'test "$(command -v limactl)" = "$HOME/tools/limactl" && ci-vm status'
            result = subprocess.run([shell, *options, command], env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Stopped', result.stdout)
        result = subprocess.run(['bash', '-c', '. "$HOME/.bashrc"; . "$HOME/.bashrc"; printf "%s" "$PATH"'], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.stdout.split(os.pathsep).count(str(self.home / '.local/bin')), 1)

    def test_install_from_extracted_archive_without_git_or_retained_source(self):
        archive = self.home / 'source.zip'
        extracted = self.home / 'extracted source'
        archive_contents = {name: (ROOT / name).read_bytes() for name in ARCHIVE_FILES}
        with zipfile.ZipFile(archive, 'w') as package:
            for name, contents in archive_contents.items():
                package.writestr(name, contents)
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
        launcher = str(self.home / '.local/bin/ci-vm')
        config = self.home / '.config/github-runner-vm/config.json'
        original = config.read_bytes()
        (self.bin / 'limactl').write_text('#!/bin/sh\nprintf called > "$HOME/unexpected-tool-call"\nexit 1\n')
        for args in ([], ['setup', 'https://github.com/owner/repo.git/', '--legacy']):
            result = subprocess.run([launcher, *args], env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('\033', result.stdout)
        self.assertIn('Connect owner/repo', result.stdout)
        self.assertIn('setup is not verified', result.stdout)
        share = self.home / '.local/share/github-runner-vm'
        self.assertIn(str(share / 'docs/setup.md'), result.stdout)
        for name in ('docs/setup.md', 'docs/maintenance.md', 'docs/security.md', 'examples/smoke.yml'):
            self.assertEqual((share / name).read_bytes(), archive_contents[name])
        installed_setup = (share / 'docs/setup.md').read_text().lower()
        for phrase in ('token handoff', 'registration handoff', 'after the user confirms registration', 'spare-mac-arm64', 'expected_runner'):
            self.assertNotIn(phrase, installed_setup)
        self.assertNotIn('EXPECTED_RUNNER', (share / 'examples/smoke.yml').read_text())
        self.assertFalse((self.home / 'unexpected-tool-call').exists())
        self.assertEqual(config.read_bytes(), original)

    def bootstrap_download(self):
        archive = self.home / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as package:
            for name in ARCHIVE_FILES:
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
        command = next(block.split('\n', 1)[1] for block in (ROOT / 'docs/setup.md').read_text().split('```')[1::2]
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
    if 'sort -u' in script:
        print('\n'.join(['ci-vm-runner.service', *json.loads(os.environ.get('FAKE_MEMBER_UNITS', '[]'))]))
    elif 'SharedSetup=' in script:
        print('SharedSetup=' + ('active' if os.environ.get('FAKE_SETUP_GATE') else 'clear'))
    elif 'echo PackageGate=' in script:
        print('PackageGate=clear')
    elif 'UnitHash' in script:
        name = args[8]
        unit = pathlib.Path.home() / '.local/share/github-runner-vm/config' / ('ci-vm-runner@.service' if '@' in name else 'ci-vm-runner.service')
        content = unit.read_bytes().replace(b'@KEY@', name.removeprefix('ci-vm-runner@').removesuffix('.service').encode())
        print('LoadState=loaded\nFragmentPath=/etc/systemd/user/' + name + '\nDropInPaths=\nNeedDaemonReload=no\nTransient=no')
        print('UnitHash=' + hashlib.sha256(content).hexdigest())
        print('UnitOwner=0:644\nActualUID=1001\nMarkerOwner=0:755')
    elif 'set_pause_marker' in script:
        pathlib.Path(os.environ['FAKE_PAUSED']).touch()
    elif 'CgroupEmpty' in script:
        phase = 'running' if args[8] == os.environ.get('FAKE_BUSY_UNIT') else os.environ.get('FAKE_PHASE', 'paused')
        print('ActiveState=' + ('inactive' if phase == 'paused' else 'active'))
        print('SubState=' + ('dead' if phase == 'paused' else 'running'))
        print('MainPID=0\nControlPID=0\nControlGroup=\nJob=0\nPaused=yes\nRunners=0\nContainers=0\nRuntimeDrift=no\nJobs=0\nCgroupEmpty=yes')
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

    def test_missing_source_guide_and_symlinked_install_guide_fail_before_writes(self):
        source = self.home / 'source'
        for name in ARCHIVE_FILES:
            if name == 'docs/setup.md':
                continue
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        result = subprocess.run(['bash', str(source / 'install.sh'), '--adopt', 'ci'], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / '.local').exists())
        self.assertEqual(self.install().returncode, 0)
        share = self.home / '.local/share/github-runner-vm'
        guide = share / 'docs/setup.md'
        original = guide.read_bytes()
        guide.unlink()
        external = self.home / 'outside.md'
        external.write_bytes(original)
        guide.symlink_to(external)
        module = share / 'ci_vm.py'
        module_before = module.read_bytes()
        self.assertNotEqual(self.install().returncode, 0)
        self.assertTrue(guide.is_symlink())
        self.assertEqual(external.read_bytes(), original)
        self.assertEqual(module.read_bytes(), module_before)

    def test_missing_configuration_uses_only_safe_bundled_setup_guide(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            guide = root / 'docs/setup.md'
            guide.parent.mkdir()
            guide.write_text('guide')
            with patch.object(cli, 'ROOT', root), patch.object(cli, 'configurations', return_value=[]):
                with self.assertRaises(cli.Failure) as failure:
                    cli.load_config()
            self.assertIn(str(guide), str(failure.exception))
            self.assertNotIn('f9c4ecff93db418e7df7c2f157b04f54c6313474', str(failure.exception))
            guide.unlink()
            guide.symlink_to(root / 'outside.md')
            with patch.object(cli, 'ROOT', root), patch.object(cli, 'configurations', return_value=[]):
                with self.assertRaises(cli.Failure) as failure:
                    cli.load_config()
            self.assertIn('reinstall', str(failure.exception).lower())

    def test_ci_checks_every_shipped_shell_helper_and_committed_whitespace(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('bash -n install.sh config/provision.sh config/prepare-shared-runner.sh config/container-runtime-state.sh', workflow)
        self.assertIn('git diff --check "$(git hash-object -t tree /dev/null)" HEAD', workflow)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            subprocess.run(['git', 'init', '--quiet'], cwd=root, check=True)
            (root / 'bad.txt').write_text('trailing space \n')
            subprocess.run(['git', 'add', 'bad.txt'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'test'], cwd=root, check=True)
            result = subprocess.run(['git', 'diff', '--check', subprocess.check_output(['git', 'hash-object', '-t', 'tree', '/dev/null'], cwd=root, text=True).strip(), 'HEAD'], cwd=root, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_runner_units_reject_regular_and_symlinked_pause_markers(self):
        condition = '/usr/bin/test ! -L /var/lib/ci-vm/paused'
        for name in ('ci-vm-runner.service', 'ci-vm-runner@.service'):
            self.assertIn('ExecCondition=/usr/bin/test ! -e /var/lib/ci-vm/paused', (ROOT / 'config' / name).read_text())
            self.assertIn('ExecCondition=' + condition, (ROOT / 'config' / name).read_text())
        with tempfile.TemporaryDirectory() as name:
            marker = Path(name) / 'paused'
            for kind in ('absent', 'file', 'live-symlink', 'dangling-symlink'):
                if marker.exists() or marker.is_symlink():
                    marker.unlink()
                if kind == 'file':
                    marker.touch()
                elif kind == 'live-symlink':
                    target = Path(name) / 'target'
                    target.touch()
                    marker.symlink_to(target)
                elif kind == 'dangling-symlink':
                    marker.symlink_to(Path(name) / 'missing')
                result = subprocess.run(['sh', '-c', 'test ! -e "$1" && test ! -L "$1"', 'sh', str(marker)])
                self.assertEqual(result.returncode, 0 if kind == 'absent' else 1, kind)

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

    def test_repository_profiles_route_only_the_selected_vm_and_preserve_legacy(self):
        fake = self.bin / 'limactl'
        fake.write_text('#!' + sys.executable + '\n' + '''import json, os, pathlib, sys
with (pathlib.Path.home() / 'profile-calls').open('a') as calls:
    calls.write(json.dumps([os.environ['LIMA_HOME'], sys.argv[1:]]) + '\\n')
if sys.argv[1:] != ['list', '--json']:
    sys.exit(2)
print(json.dumps([dict(name=name, status='Stopped') for name in ('ci', 'one', 'two')]))
''')
        self.assertEqual(self.install().returncode, 0)
        legacy = self.home / '.config/github-runner-vm/config.json'
        before = (legacy.read_bytes(), legacy.stat().st_mtime_ns)
        for repo, vm in (('owner/one', 'one'), ('owner/two', 'two')):
            result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--adopt', vm, '--repo', repo], env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((legacy.read_bytes(), legacy.stat().st_mtime_ns), before)
        launcher = str(self.home / '.local/bin/ci-vm')
        for args, expected in ((['--repo', 'owner/one', 'status'], 'one'), (['status', '--repo', 'owner/two'], 'two'), (['status', '--legacy'], 'ci')):
            result = subprocess.run([launcher, *args], env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f'VM {expected}: Stopped\n')
        calls = self.home / 'profile-calls'
        before_calls = calls.read_bytes()
        for args in (['profiles'], ['setup', 'owner/one']):
            result = subprocess.run([launcher, *args], env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('VM one', result.stdout)
        self.assertIn('ci-vm --repo owner/one status', result.stdout)
        for args in (['status'], ['status', '--repo', 'owner/missing'], ['setup', 'owner/missing']):
            result = subprocess.run([launcher, *args], env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(calls.read_bytes(), before_calls)

    def test_duplicate_physical_vm_binding_refused_through_lima_home_alias(self):
        self.assertEqual(self.install('--repo', 'owner/one').returncode, 0)
        real = self.home / '.lima'
        real.mkdir()
        alias = self.home / 'lima-alias'
        alias.symlink_to(real, target_is_directory=True)
        result = self.install('--repo', 'owner/two', '--lima-home', str(alias))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('already bound', result.stderr)
        profiles = self.home / '.config/github-runner-vm/profiles'
        self.assertEqual(len(list(profiles.glob('*.json'))), 1)
        result = self.install()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((profiles.parent / 'config.json').exists())

    def test_profile_filename_permissions_symlinks_and_duplicate_keys_refused(self):
        self.assertEqual(self.install('--repo', 'Owner/Repo').returncode, 0)
        directory = self.home / '.config/github-runner-vm/profiles'
        profile = directory / (cli.profile_key('owner/repo') + '.json')
        original = profile.read_bytes()
        launcher = str(self.home / '.local/bin/ci-vm')
        for mutation in ('filename', 'permissions', 'symlink', 'duplicate', 'unnormalized', 'injection'):
            with self.subTest(mutation=mutation):
                wrong = directory / 'wrong.json'
                external = self.home / 'profile.json'
                if mutation == 'filename':
                    profile.rename(wrong)
                elif mutation == 'permissions':
                    profile.chmod(0o644)
                elif mutation == 'symlink':
                    profile.rename(external)
                    profile.symlink_to(external)
                elif mutation == 'duplicate':
                    profile.write_bytes(original.replace(b'"version": 2,', b'"version": 2, "version": 2,'))
                elif mutation == 'unnormalized':
                    profile.write_bytes(original.replace(b'owner/repo', b'Owner/Repo'))
                else:
                    profile.write_bytes(original.replace(b'"vm": "ci"', b'"vm": "../ci"'))
                result = subprocess.run([launcher, 'profiles'], env=self.env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                if mutation == 'filename':
                    wrong.rename(profile)
                elif mutation == 'symlink':
                    profile.unlink()
                    external.rename(profile)
                profile.chmod(0o600)
                profile.write_bytes(original)

    def test_legacy_bound_repository_selection_stays_compatible(self):
        self.assertEqual(self.install().returncode, 0)
        legacy = self.home / '.config/github-runner-vm/config.json'
        config = json.loads(legacy.read_text())
        config.update(repo='Owner/Repo', runner_id=7)
        legacy.write_text(json.dumps(config))
        original = legacy.read_bytes()
        result = subprocess.run([str(self.home / '.local/bin/ci-vm'), 'status', '--repo', 'owner/repo'], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'VM ci: Stopped\n')
        self.assertEqual(legacy.read_bytes(), original)
        timestamp = legacy.stat().st_mtime_ns
        result = self.install('--repo', 'owner/repo', '--runner-id', '7', '--configure-shell')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('setup Owner/Repo', result.stdout)
        self.assertIn('"$HOME/.local/bin/ci-vm" --repo Owner/Repo status', result.stdout)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(legacy.stat().st_mtime_ns, timestamp)
        self.assertFalse((legacy.parent / 'profiles').exists())

    def test_case_aliases_cannot_bind_the_same_physical_vm_twice(self):
        actual = self.home / '.lima'
        actual.mkdir()
        alias = self.home / '.LIMA'
        if not alias.exists() or not actual.samefile(alias):
            self.skipTest('Temporary filesystem is case-sensitive.')
        (actual / 'ci').mkdir()
        self.assertEqual(self.install('--repo', 'owner/one').returncode, 0)
        result = self.install('--repo', 'owner/two', '--lima-home', str(alias))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('already bound', result.stderr)

    def test_explicit_shared_attachment_rerun_and_cross_owner_selection(self):
        self.assertEqual(self.install('--repo', 'owner/one').returncode, 0)
        self.fake_running_tools()
        command = ['bash', str(ROOT / 'install.sh'), '--repo', 'another/two', '--share-with', 'owner/one']
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.home / '.config/github-runner-vm/profiles' / (cli.profile_key('another/two') + '.json')
        original = (path.read_bytes(), path.stat().st_mtime_ns)
        profile = json.loads(original[0])
        self.assertEqual(profile['version'], 3)
        self.assertEqual(profile['shared_with'], 'owner/one')
        self.assertEqual(profile['unit'], cli.member_unit('another/two'))
        self.assertNotIn('runner_id', profile)
        before = (self.home / 'calls').read_bytes()
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(original, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(before, (self.home / 'calls').read_bytes())
        anchor_path = path.parent / (cli.profile_key('owner/one') + '.json')
        anchor_before = (anchor_path.read_bytes(), anchor_path.stat().st_mtime_ns)
        result = self.install('--repo', 'owner/one')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(anchor_before, (anchor_path.read_bytes(), anchor_path.stat().st_mtime_ns))
        self.env['FAKE_MEMBER_UNITS'] = json.dumps([profile['unit']])
        launcher = str(self.home / '.local/bin/ci-vm')
        result = subprocess.run([launcher, 'setup', 'another/two'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('resume --all-repos', result.stdout)
        self.assertIn('/home/ci/runners/' + cli.profile_key('another/two'), result.stdout)
        for operation in ('pause', 'resume', 'restart'):
            result = subprocess.run([launcher, '--repo', 'another/two', operation], env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('--all-repos', result.stderr)
        result = subprocess.run([launcher, '--repo', 'another/two', 'pause', '--all-repos'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('owner/one', result.stdout)
        self.assertIn('another/two', result.stdout)

    def test_shared_attachment_requires_existing_idle_anchor_and_valid_reference(self):
        self.assertEqual(self.install('--repo', 'owner/one').returncode, 0)
        command = ['bash', str(ROOT / 'install.sh'), '--repo', 'another/two', '--share-with', 'owner/one']
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 3)
        self.assertIn('Running', result.stderr)
        self.fake_running_tools()
        self.env['FAKE_PHASE'] = 'running'
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 3)
        self.assertIn('Pause', result.stderr)
        self.env.pop('FAKE_PHASE')
        for extra in (['--memory', '4'], ['--unit', 'custom.service'], ['--lima-home', str(self.home / 'other')]):
            result = subprocess.run(command + extra, env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(subprocess.run(command, env=self.env, capture_output=True, timeout=10).returncode, 0)
        directory = self.home / '.config/github-runner-vm/profiles'
        member = directory / (cli.profile_key('another/two') + '.json')
        original = member.read_text()
        for value in ('missing/repo', 'another/two', True, 17, []):
            data = json.loads(original)
            data['shared_with'] = value
            member.write_text(json.dumps(data))
            result = subprocess.run([str(self.home / '.local/bin/ci-vm'), 'profiles'], env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        member.write_text(original)
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--repo', 'third/repo', '--share-with', 'another/two'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(version=1, vm='ci', lima_home='/tmp/lima', guest_user='ci', guest_uid=1001, unit='ci-vm-runner.service')
        gate = patch.object(cli, 'shared_setup_gate', return_value=False)
        gate.start()
        self.addCleanup(gate.stop)

    def shared_selection(self):
        anchor = dict(self.config, version=2, repo='owner/one')
        member = dict(self.config, version=3, repo='other/two', shared_with='owner/one', unit=cli.member_unit('other/two'))
        return cli.Selection(member, (anchor, member))

    def test_shared_inventory_and_secondary_cgroups_refuse_mutation(self):
        selection = self.shared_selection()
        names = [member['unit'] for member in selection.members]
        for inventory in (names + ['ci-vm-runner@orphan-000000000000.service'], names[:1], names + names[:1]):
            with self.subTest(inventory=inventory), patch.object(cli, 'verify_contract'), patch.object(cli, 'guest', return_value='\n'.join(inventory) + '\n'):
                with self.assertRaises(cli.Failure):
                    cli.group_contract(selection, cli.deadline(1))
        with patch.object(cli, 'verify_contract'), patch.object(cli, 'guest', return_value='\n'.join(names) + '\n') as guest:
            cli.group_contract(selection, cli.deadline(1))
            script = guest.call_args.args[1]
            self.assertIn('persistent=$(', script)
            self.assertIn('loaded=$(', script)
            self.assertIn('enabled=$(', script)
        for secondary in ({'CgroupEmpty': 'no'}, {'ActiveState': 'active'}, {'Runners': '1'}):
            with self.subTest(secondary=secondary), patch.object(cli, 'guest', side_effect=[self.idle_output(), self.idle_output(**secondary)]) as guest:
                self.assertFalse(cli.group_idle(selection, cli.deadline(1)))
                self.assertEqual([call.args[0]['unit'] for call in guest.call_args_list], names)
            with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'package_gate', return_value=False), patch.object(cli, 'guest', side_effect=[self.idle_output(), self.idle_output(**secondary)]), patch.object(cli, 'lima') as lima, patch('sys.stdout', io.StringIO()):
                with self.assertRaises(cli.Failure):
                    cli.maintain(selection, 'restart', cli.deadline(1), all_repos=True)
                lima.assert_not_called()

    def test_dedicated_resume_validates_pause_marker_before_removal(self):
        profile = dict(self.config, version=2, repo='owner/repo')
        selection = cli.Selection(profile, (profile,))
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), \
                patch.object(cli, 'package_gate', return_value=False), patch.object(cli, 'shared_setup_gate', return_value=False), \
                patch.object(cli, 'guest_mutation') as mutation, patch('sys.stdout', io.StringIO()):
            cli.maintain(selection, 'resume', cli.deadline(1))
        script = mutation.call_args.args[1]
        self.assertLess(script.index('validate_pause_marker\nshift 3'), script.index('rm -f -- /var/lib/ci-vm/paused'))

    def test_shared_resume_failure_restores_pause_and_setup_gate_blocks(self):
        selection = self.shared_selection()
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'package_gate', return_value=False), patch.object(cli, 'shared_setup_gate', return_value=True), patch.object(cli, 'guest_mutation') as mutation, patch('sys.stdout', io.StringIO()):
            for operation in ('resume', 'restart'):
                with self.assertRaises(cli.Failure) as result:
                    cli.maintain(selection, operation, cli.deadline(1), all_repos=True)
                self.assertIn('Shared setup is unfinished', str(result.exception))
            mutation.assert_not_called()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'paused').touch()
            unit = selection['unit']
            header = '''set -euo pipefail
sudo() { shift; "$@"; }
stat() { case "$3" in */paused) echo 0:644;; *) echo 0:755;; esac; }
chown() { :; }
ctl() {
    if test "$1" = is-enabled; then echo enabled; return; fi
    if test "$1" = start; then
        printf '%s\\n' "$3" >> STARTS
        test "$3" != SECOND
    fi
}
'''.replace('STARTS', str(root / 'starts')).replace('SECOND', unit)
            def mutate(config, script, until, *args):
                result = subprocess.run(['bash', '-s', '--', 'ci', '1001', unit, *args], input=script.replace(cli.USER_ENV, header).replace('/var/lib/ci-vm', str(root)), text=True, capture_output=True, timeout=5)
                if result.returncode:
                    raise cli.Failure('synthetic second start failed')
                return result.stdout
            with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'group_idle', return_value=True), patch.object(cli, 'package_gate', return_value=False), patch.object(cli, 'guest_mutation', side_effect=mutate), patch('sys.stdout', io.StringIO()):
                with self.assertRaises(cli.Failure) as result:
                    cli.maintain(selection, 'resume', cli.deadline(1), all_repos=True)
                self.assertIn('Pause marker restored', str(result.exception))
            self.assertTrue((root / 'paused').exists())
            self.assertEqual((root / 'starts').read_text().splitlines(), [member['unit'] for member in selection.members])
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'group_idle', return_value=False), patch.object(cli, 'package_gate', return_value=False), patch.object(cli, 'guest_mutation') as mutation, patch('sys.stdout', io.StringIO()):
            with self.assertRaises(cli.Failure):
                cli.maintain(selection, 'resume', cli.deadline(1), all_repos=True)
            mutation.assert_not_called()

    def test_shared_missing_contract_pause_retains_gate_and_reports_pending(self):
        selection = self.shared_selection()
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract', side_effect=cli.Failure('missing member', 3)), patch.object(cli, 'guest_mutation') as mutation, patch('sys.stdout', io.StringIO()):
            with self.assertRaises(cli.Failure) as result:
                cli.maintain(selection, 'pause', cli.deadline(1), all_repos=True)
            self.assertEqual(result.exception.code, 4)
            self.assertIn('set_pause_marker', mutation.call_args.args[1])

    def test_pause_marker_helper_rejects_symlinks_and_creates_exact_marker(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o755)
            target = root / 'target'
            target.write_text('unchanged')
            marker = root / 'paused'
            marker.symlink_to(target)
            platform_helpers = '''set -euo pipefail
stat() { case "$3" in */paused) echo 0:644;; *) echo 0:755;; esac; }
chown() { :; }
'''
            script = platform_helpers + cli.PAUSE_MARKER.replace('/var/lib/ci-vm', str(root)) + 'set_pause_marker\n'
            refused = subprocess.run(['bash', '-s'], input=script, text=True, capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(target.read_text(), 'unchanged')
            marker.unlink()
            created = subprocess.run(['bash', '-s'], input=script, text=True, capture_output=True)
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertTrue(marker.is_file())
            self.assertFalse(marker.is_symlink())
            self.assertEqual(marker.stat().st_mode & 0o777, 0o644)

    def test_overview_and_help_need_no_configuration_or_subprocess(self):
        for args in ([], ['--help'], ['setup', '--help']):
            output = io.StringIO()
            with self.subTest(args=args), patch.object(cli, 'load_config') as load, patch.object(cli, 'run') as run, patch('sys.stdout', output):
                if '--help' in args:
                    with self.assertRaises(SystemExit) as result:
                        cli.main(args)
                    self.assertEqual(result.exception.code, 0)
                else:
                    self.assertEqual(cli.main(args), 0)
                load.assert_not_called()
                run.assert_not_called()
                self.assertIn('OWNER/REPO' if args != ['--help'] else 'setup', output.getvalue())
                self.assertNotIn('\033', output.getvalue())

    def test_setup_uses_selected_identity_without_mutations_or_probes(self):
        config = dict(self.config, vm='build-vm', lima_home="/tmp/runner's home")
        output = io.StringIO()
        with patch.object(cli, 'load_config', return_value=config), patch.object(cli, 'run') as run, patch.object(cli, 'atomic_write') as write, patch('sys.stdout', output):
            self.assertEqual(cli.main(['setup', 'https://github.com/owner/repo.git/']), 0)
        run.assert_not_called()
        write.assert_not_called()
        text = output.getvalue()
        self.assertIn('Connect owner/repo', text)
        self.assertIn('ci-vm --legacy register --manual-token owner/repo', text)
        self.assertIn('runs-on: [self-hosted, Linux, ARM64, spare-mac]', text)
        self.assertIn('independently verified service contract', text)
        self.assertIn('No commands below were executed; setup is not verified.', text)
        self.assertIn('Never paste a runner token into chat.', text)

    def test_repository_setup_guide_reports_inactive_enable_then_doctor_resume_status(self):
        profile = dict(self.config, version=2, repo='owner/repo')
        output = io.StringIO()
        with patch('sys.stdout', output):
            cli.setup_guide(profile, profile['repo'])
        service = output.getvalue().split('3. Verify, then resume the runner service', 1)[1].split('4. Verify', 1)[0]
        self.assertIn('register already enabled the exact service without starting it', service)
        self.assertLess(service.index('ci-vm --repo owner/repo doctor'), service.index('ci-vm --repo owner/repo resume'))
        self.assertLess(service.index('ci-vm --repo owner/repo resume'), service.index('ci-vm --repo owner/repo status'))
        self.assertNotIn('token handoff', output.getvalue().lower())

    def test_normal_setup_documents_exclude_retired_manual_registration_flow(self):
        normal = '\n'.join((ROOT / path).read_text() for path in (
            'README.md', 'docs/agent-walkthrough.md', 'docs/maintenance.md',
            '.agents/skills/verify-github-runner-vm/features/maintenance.md',
            '.agents/skills/verify-github-runner-vm/features/profiles.md',
        ))
        for phrase in (
            'token handoff', 'registration handoff', 'uncaptured registration',
            'after the user confirms registration', 'separate exact github readback',
            'spare-mac-arm64', 'expected_runner',
        ):
            self.assertNotIn(phrase, normal.lower())
        setup = (ROOT / 'docs/setup.md').read_text()
        shared = setup.split('## Share an existing repository VM', 1)[1].split('## Select the repository VM', 1)[0]
        self.assertNotIn('systemctl --user enable', shared)
        self.assertNotIn('prepare-shared-runner.sh" finish', shared)
        self.assertIn('resume --all-repos', shared)
        fallback = setup.split('### Legacy manual fallback', 1)[1].split('## Verify, then resume', 1)[0]
        self.assertIn('--manual-token', fallback)
        smoke = (ROOT / 'examples/smoke.yml').read_text()
        self.assertNotIn('EXPECTED_RUNNER', smoke)
        self.assertNotIn('spare-mac-arm64', smoke)

    def test_managed_setup_guide_reconciles_existing_registration(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            cli.setup_guide(dict(self.config, version=2, repo='owner/repo'), 'owner/repo')
        text = output.getvalue()
        self.assertIn('Preserve them and rerun register', text)
        self.assertNotIn('skip this step', text)

    def test_setup_rejects_unsafe_repository_before_reading_configuration(self):
        for repo in ('owner/repo;id', 'https://github.com/owner/repo?token=secret', 'https://evil.test/owner/repo', 'owner/../repo', '../repo', 'owner/repo\n', 'https://github.com/owner/repo/actions'):
            with self.subTest(repo=repo), patch.object(cli, 'load_config') as load, patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    cli.main(['setup', repo])
                self.assertEqual(result.exception.code, 2)
                load.assert_not_called()

    def test_register_opens_only_the_interactive_token_prompt(self):
        profile = dict(self.config, version=2, repo='owner/chat', vm='chat-vm', lima_home='/tmp/lima')
        selection = cli.Selection(profile, (profile,))
        terminal = Mock()
        terminal.isatty.return_value = True
        completed = Mock(returncode=0)
        with patch('sys.stdin', terminal), patch('sys.stdout', terminal), patch('sys.stderr', terminal), \
                patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'ensure_no_package_work') as packages, \
                patch.object(cli, 'complete_setup') as setup, patch.object(cli, 'group_contract') as contract, \
                patch.object(cli, 'group_idle', return_value=True), \
                patch.object(cli.subprocess, 'Popen', return_value=completed) as operation:
            cli.register_runner(selection, cli.deadline(1), manual_token=True)
        packages.assert_called_once()
        setup.assert_called_once()
        contract.assert_called_once()
        argv = operation.call_args.args[0]
        self.assertEqual(argv[:4], ['limactl', 'shell', 'chat-vm', '--'])
        self.assertIn('/home/ci/actions-runner', argv)
        self.assertIn('/home/ci/work/actions', argv)
        self.assertIn('https://github.com/owner/chat', argv)
        expected_identity = hashlib.sha256(b'/tmp/lima\0chat-vm').hexdigest()[:8]
        self.assertIn('chat-' + expected_identity + '-arm64', argv)
        self.assertIn('spare-mac,chat-ci', argv)
        self.assertFalse(any('token' in value.lower() for value in argv))
        self.assertEqual(operation.call_args.kwargs['env']['LIMA_HOME'], '/tmp/lima')

    def test_register_refuses_capture_wrong_state_and_missing_shared_gate(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        selection = cli.Selection(profile, (profile,))
        terminal = Mock()
        terminal.isatty.return_value = False
        with patch('sys.stdin', terminal), patch('sys.stdout', terminal), patch('sys.stderr', terminal), patch.object(cli, 'vm_state') as state:
            with self.assertRaises(cli.Failure) as failure:
                cli.register_runner(selection, cli.deadline(1), manual_token=True)
            self.assertIn('own interactive terminal', str(failure.exception))
            state.assert_not_called()
        terminal.isatty.return_value = True
        member = dict(profile, version=3, shared_with='owner/one', unit=cli.member_unit('owner/chat'))
        shared = cli.Selection(member, (dict(profile, repo='owner/one'), member))
        with patch('sys.stdin', terminal), patch('sys.stdout', terminal), patch('sys.stderr', terminal), \
                patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'ensure_no_package_work'), \
                patch.object(cli, 'shared_setup_gate', return_value=False), patch.object(cli, 'group_contract') as contract:
            with self.assertRaises(cli.Failure) as failure:
                cli.register_runner(shared, cli.deadline(1), manual_token=True, all_repos=True)
            self.assertIn('preparation gate is missing', str(failure.exception))
            contract.assert_not_called()

    def test_manual_registration_uses_inherited_terminal_and_deadline(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        target = cli.registration_target(profile)
        process = unittest.mock.Mock(returncode=0)
        with patch.object(cli.sys.stdin, 'isatty', return_value=True), patch.object(cli.sys.stdout, 'isatty', return_value=True), \
                patch.object(cli.sys.stderr, 'isatty', return_value=True), patch.object(cli, 'vm_state', return_value='Running'), \
                patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'complete_setup'), patch.object(cli, 'group_contract'), \
                patch.object(cli, 'group_idle', return_value=True), patch.object(cli.subprocess, 'Popen', return_value=process) as popen:
            cli.manual_register_runner(profile, target, cli.deadline(1))
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertNotIn('stdin', popen.call_args.kwargs)
        self.assertNotIn('stdout', popen.call_args.kwargs)
        self.assertNotIn('stderr', popen.call_args.kwargs)
        self.assertGreater(process.wait.call_args.kwargs['timeout'], 0)
        timeout = unittest.mock.Mock()
        timeout.wait.side_effect = subprocess.TimeoutExpired(['limactl'], 1)
        with patch.object(cli.sys.stdin, 'isatty', return_value=True), patch.object(cli.sys.stdout, 'isatty', return_value=True), \
                patch.object(cli.sys.stderr, 'isatty', return_value=True), patch.object(cli, 'vm_state', return_value='Running'), \
                patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'complete_setup'), patch.object(cli, 'group_contract'), \
                patch.object(cli, 'group_idle', return_value=True), patch.object(cli.subprocess, 'Popen', return_value=timeout), \
                patch.object(cli.os, 'killpg') as killpg:
            with self.assertRaises(cli.Failure) as failure:
                cli.manual_register_runner(profile, target, cli.deadline(1))
        self.assertEqual(failure.exception.code, 4)
        self.assertIn('unconfirmed', str(failure.exception))
        killpg.assert_called_once()

    def test_manual_registration_forwards_interrupt_and_restores_signal_handlers(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        target = cli.registration_target(profile)
        process = unittest.mock.Mock(returncode=-signal.SIGINT, pid=2468)
        handlers = {}
        originals = {signum: object() for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

        def install(signum, handler):
            previous = handlers.get(signum, originals[signum])
            handlers[signum] = handler
            return previous

        def wait(timeout):
            if callable(handlers[signal.SIGINT]):
                handlers[signal.SIGINT](signal.SIGINT, None)

        process.wait.side_effect = wait
        with patch.object(cli.sys.stdin, 'isatty', return_value=True), patch.object(cli.sys.stdout, 'isatty', return_value=True), \
                patch.object(cli.sys.stderr, 'isatty', return_value=True), patch.object(cli, 'vm_state', return_value='Running'), \
                patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'complete_setup'), patch.object(cli, 'group_contract'), \
                patch.object(cli, 'group_idle', return_value=True), patch.object(cli.subprocess, 'Popen', return_value=process), \
                patch.object(cli.signal, 'signal', side_effect=install), patch.object(cli.os, 'killpg') as killpg:
            with self.assertRaises(cli.Failure) as failure:
                cli.manual_register_runner(profile, target, cli.deadline(1))
        self.assertEqual(failure.exception.code, 4)
        self.assertIn('interrupted', str(failure.exception))
        calls = [(item.args, item.kwargs) for item in killpg.call_args_list]
        self.assertIn(((2468, signal.SIGINT), {}), calls)
        self.assertIn(((2468, signal.SIGKILL), {}), calls)
        self.assertEqual({signum: handlers[signum] for signum in originals}, originals)

    def test_common_command_and_manual_launch_forward_early_interrupts(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                process = unittest.mock.Mock(returncode=-signal.SIGINT, pid=2468)
                process.communicate.return_value = ('', '')
                handlers = {}
                originals = {signum: object() for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

                def install(signum, handler):
                    previous = handlers.get(signum, originals[signum])
                    handlers[signum] = handler
                    return previous

                def launch(*args, **kwargs):
                    handlers[signal.SIGINT](signal.SIGINT, None)
                    return process

                patches = [patch.object(cli.signal, 'signal', side_effect=install),
                           patch.object(cli.subprocess, 'Popen', side_effect=launch),
                           patch.object(cli.os, 'killpg')]
                if manual:
                    profile = dict(self.config, version=2, repo='owner/chat')
                    target = cli.registration_target(profile)
                    patches.extend((patch.object(cli.sys.stdin, 'isatty', return_value=True),
                                    patch.object(cli.sys.stdout, 'isatty', return_value=True),
                                    patch.object(cli.sys.stderr, 'isatty', return_value=True),
                                    patch.object(cli, 'vm_state', return_value='Running'),
                                    patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'complete_setup'),
                                    patch.object(cli, 'group_contract'), patch.object(cli, 'group_idle', return_value=True)))
                entered = [item.__enter__() for item in patches]
                try:
                    with self.assertRaises(cli.Failure) as failure:
                        if manual:
                            cli.manual_register_runner(profile, target, cli.deadline(1))
                        else:
                            cli.run(['synthetic'], cli.deadline(1))
                finally:
                    for item in reversed(patches):
                        item.__exit__(None, None, None)
                self.assertEqual(failure.exception.code, 4)
                calls = [(item.args, item.kwargs) for item in entered[2].call_args_list]
                self.assertIn(((2468, signal.SIGINT), {}), calls)
                self.assertIn(((2468, signal.SIGKILL), {}), calls)

    def test_register_command_routes_selected_repository(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        selection = cli.Selection(profile, (profile,))
        with patch.object(cli, 'load_config', return_value=selection), patch.object(cli, 'register_runner') as register, patch.object(cli, 'operation_lock'):
            self.assertEqual(cli.main(['--repo', 'owner/chat', 'register']), 0)
        self.assertIs(register.call_args.args[0], selection)
        self.assertIsNone(register.call_args.args[2])
        self.assertFalse(register.call_args.args[3])

    def registration_evidence(self, profile, runner_id=41):
        target = cli.registration_target(profile)
        local = cli.LocalRegistration(runner_id, target.name, target.url, target.work_directory)
        remote = cli.RemoteRegistration(runner_id, target.name, 'offline', False,
                                        ('self-hosted', 'Linux', 'ARM64', *target.labels))
        return target, local, remote

    def test_unattended_registration_transports_token_only_through_stdin(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        target = cli.registration_target(profile)
        token = 'synthetic-registration-token-12345'
        with patch.object(cli, 'lima') as lima:
            cli.configure_runner(profile, target, token, cli.deadline(1))
        argv = lima.call_args.args[1]
        self.assertNotIn(token, argv)
        self.assertNotIn(token, ' '.join(argv))
        self.assertEqual(argv[:3], ['shell', '--tty=false', 'ci'])
        self.assertIn(['bash', '-s', '--'], [argv[index:index + 3] for index in range(len(argv) - 2)])
        self.assertNotIn('-c', argv)
        separator = argv.index('--', argv.index('-s'))
        self.assertEqual(argv[separator + 1], target.runner_directory)
        self.assertIn(target.url, argv)
        guest_script = lima.call_args.kwargs['input']
        self.assertEqual(guest_script.splitlines().count(token), 1)
        self.assertIn('ACTIONS_RUNNER_INPUT_TOKEN', guest_script)
        self.assertIn('exec ./config.sh --unattended "$@"', guest_script)
        self.assertLess(guest_script.index('umask 077'), guest_script.index('exec ./config.sh --unattended'))
        self.assertNotIn('--token', argv)

    def test_registration_readback_requires_exact_unique_label_set(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        target, _, remote = self.registration_evidence(profile)
        labels = ('ARM64', target.labels[1].upper(), 'Linux', 'self-hosted', target.labels[0].upper())
        self.assertTrue(cli.valid_remote(target, cli.RemoteRegistration(remote.runner_id, remote.name, remote.status, remote.busy, labels)))
        for observed in (
            labels[1:], labels + ('extra',), labels + ('linux',), labels + ('',),
            labels + ('bad\x00label',), labels + ('x' * 257,),
        ):
            with self.subTest(observed=observed):
                runner = cli.RemoteRegistration(remote.runner_id, remote.name, remote.status, remote.busy, observed)
                self.assertFalse(cli.valid_remote(target, runner))

    def test_registration_token_heredoc_rejects_delimiter_collision_and_multiline_values(self):
        token = 'CI_VM_REGISTRATION_TOKEN_CI_VM_REGISTRATION_TOKEN_'
        script = cli.registration_token_script(token)
        marker_line = next(line for line in script.splitlines() if "<<'" in line)
        marker = marker_line.split("<<'", 1)[1].removesuffix("'")
        self.assertNotIn(marker, token)
        self.assertEqual(script.splitlines().count(token), 1)
        for invalid in ('', 'token\nsecond', ' token', 'token '):
            with self.subTest(invalid=invalid), self.assertRaises(cli.Failure):
                cli.registration_token_script(invalid)

    def test_unattended_register_create_recover_noop_and_refuse(self):
        base = dict(self.config, version=2, repo='owner/chat')
        target, local, remote = self.registration_evidence(base)
        common = (patch.object(cli, 'github_auth'), patch.object(cli, 'vm_state', return_value='Running'),
                  patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'group_contract'),
                  patch.object(cli, 'group_idle', return_value=True), patch.object(cli, 'enable_registration_unit'))
        with common[0], common[1], common[2], common[3], common[4], common[5], \
                patch.object(cli, 'local_registration', side_effect=[None, local]), \
                patch.object(cli, 'github_runners', side_effect=[(), (remote,), (remote,)]), \
                patch.object(cli, 'registration_token', return_value='synthetic-registration-token-12345') as credential, \
                patch.object(cli, 'configure_runner') as configure, patch.object(cli, 'persist_runner_id') as persist, \
                patch('sys.stdout', io.StringIO()):
            cli.register_runner(cli.Selection(base, (base,)), cli.deadline(1))
        credential.assert_called_once()
        configure.assert_called_once()
        persist.assert_called_once_with(unittest.mock.ANY, remote.runner_id)

        for profile, outcome in ((base, 'recover'), ({**base, 'runner_id': remote.runner_id}, 'done')):
            with self.subTest(outcome=outcome), patch.object(cli, 'github_auth'), patch.object(cli, 'vm_state', return_value='Running'), \
                    patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'group_contract'), \
                    patch.object(cli, 'group_idle', return_value=True), patch.object(cli, 'local_registration', return_value=local), \
                    patch.object(cli, 'github_runners', return_value=(remote,)), patch.object(cli, 'registration_token') as credential, \
                    patch.object(cli, 'configure_runner') as configure, patch.object(cli, 'persist_runner_id') as persist, \
                    patch.object(cli, 'enable_registration_unit'), patch('sys.stdout', io.StringIO()):
                cli.register_runner(cli.Selection(profile, (profile,)), cli.deadline(1))
            credential.assert_not_called()
            configure.assert_not_called()
            persist.assert_called_once_with(unittest.mock.ANY, remote.runner_id)

        with patch.object(cli, 'github_auth'), patch.object(cli, 'vm_state', return_value='Running'), \
                patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'group_contract'), \
                patch.object(cli, 'group_idle', return_value=True), patch.object(cli, 'local_registration', return_value=None), \
                patch.object(cli, 'github_runners', return_value=(remote,)), patch.object(cli, 'registration_token') as credential:
            with self.assertRaises(cli.Failure) as failure:
                cli.register_runner(cli.Selection(base, (base,)), cli.deadline(1))
        self.assertIn('partial, mismatched', str(failure.exception))
        credential.assert_not_called()

    def test_registration_persists_only_exact_runner_id_with_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            profile = dict(self.config, version=2, repo='owner/chat', lima_home=str(home / 'lima'))
            path = home / '.config/github-runner-vm/profiles' / (cli.profile_key(profile['repo']) + '.json')
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(profile, indent=2) + '\n')
            path.chmod(0o600)
            with patch.dict(os.environ, HOME=name):
                cli.persist_runner_id(profile, 41)
                cli.persist_runner_id({**profile, 'runner_id': 41}, 41)
            self.assertEqual(json.loads(path.read_text()), {**profile, 'runner_id': 41})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('token', path.read_text().lower())
            with patch.dict(os.environ, HOME=name):
                with self.assertRaises(cli.Failure):
                    cli.persist_runner_id(profile, 42)

    def test_registration_enables_inactive_unit_and_finishes_shared_gate(self):
        member = self.shared_selection()
        target, local, remote = self.registration_evidence(member)
        order = []
        with patch.object(cli, 'github_auth'), patch.object(cli, 'vm_state', return_value='Running'), \
                patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'group_contract'), \
                patch.object(cli, 'group_idle', return_value=True), patch.object(cli, 'shared_setup_gate', side_effect=[True, True, False]), \
                patch.object(cli, 'local_registration', return_value=local), patch.object(cli, 'github_runners', return_value=(remote,)), \
                patch.object(cli, 'persist_runner_id'), patch.object(cli, 'enable_registration_unit', side_effect=lambda *_: order.append('enable')), \
                patch.object(cli, 'finish_shared_registration', side_effect=lambda *_: order.append('finish')), patch('sys.stdout', io.StringIO()):
            cli.register_runner(member, cli.deadline(1), all_repos=True)
        self.assertEqual(order, ['enable', 'finish'])

    def test_enable_registration_unit_never_starts_listener(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        target = cli.registration_target(profile)
        with patch.object(cli, 'guest_mutation') as mutation:
            cli.enable_registration_unit(profile, target, cli.deadline(1))
        script = mutation.call_args.args[1]
        self.assertIn('ctl enable "$unit"', script)
        self.assertNotIn('enable --now', script)
        self.assertNotIn('ctl start', script)

    def test_local_registration_executes_present_file_boundary_and_refuses_readable_credentials(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runner = root / 'runner'
            tools = root / 'tools'
            runner.mkdir(mode=0o700)
            tools.mkdir()
            profile = dict(self.config, version=2, repo='owner/chat', guest_uid=os.getuid())
            target = cli.registration_target(profile)
            (runner / '.runner').write_text(json.dumps({'agentId': 41, 'agentName': target.name,
                                                        'gitHubUrl': target.url, 'workFolder': target.work_directory}))
            for path in (runner / '.runner', runner / '.credentials', runner / '.credentials_rsaparams'):
                if not path.exists():
                    path.write_text('credential')
                path.chmod(0o600)
            (tools / 'stat').write_text('#!/usr/bin/env python3\nimport os,stat,sys\nf=sys.argv[sys.argv.index("-c")+1]; s=os.stat(sys.argv[-1]); u=str(s.st_uid); m=format(stat.S_IMODE(s.st_mode), "o")\nprint({"%u":u,"%a":m,"%u:%g":f"{u}:{u}","%u:%a":f"{u}:{m}","%u:%g:%a":f"{u}:{u}:{m}"}[f])\n')
            (tools / 'find').write_text('#!/usr/bin/env python3\nimport os,stat,sys\np=sys.argv[1]\nprint(p) if stat.S_IMODE(os.stat(p).st_mode) & 0o22 else None\n')
            for path in tools.iterdir():
                path.chmod(0o755)
            observed_target = cli.RegistrationTarget(target.repo, target.url, target.name, target.labels,
                                                     str(runner), target.work_directory, target.unit, target.shared_key)

            def execute(config, args, until, input=None):
                self.assertEqual(args[:10], ['shell', '--tty=false', profile['vm'], '--', 'sudo', '-iu', 'ci', 'bash', '-s', '--'])
                self.assertEqual(args[10:], [str(os.getuid()), str(runner)])
                result = subprocess.run(['bash', '-s', '--', *args[10:]],
                                        input=input, text=True, capture_output=True,
                                        env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']}, timeout=5)
                if result.returncode:
                    raise cli.Failure('synthetic local boundary refused')
                return result.stdout

            with patch.object(cli, 'lima', side_effect=execute):
                local = cli.local_registration(profile, observed_target, cli.deadline(1))
                self.assertEqual(local.runner_id, 41)
                (runner / '.credentials').chmod(0o640)
                with self.assertRaises(cli.Failure):
                    cli.local_registration(profile, observed_target, cli.deadline(1))

    def test_local_registration_treats_only_env_and_path_as_unregistered(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runner = root / 'runner'
            tools = root / 'tools'
            runner.mkdir(mode=0o700)
            tools.mkdir()
            for filename in ('.env', '.path'):
                path = runner / filename
                path.write_text('pre-registration setup')
                path.chmod(0o664)
            stat_tool = tools / 'stat'
            stat_tool.write_text('#!/usr/bin/env python3\nimport os,stat,sys\ns=os.stat(sys.argv[-1]); u=str(s.st_uid); m=format(stat.S_IMODE(s.st_mode), "o")\nprint(f"{u}:{u}:{m}")\n')
            stat_tool.chmod(0o755)
            profile = dict(self.config, version=2, repo='owner/chat', guest_uid=os.getuid())
            target = cli.registration_target(profile)
            observed_target = cli.RegistrationTarget(target.repo, target.url, target.name, target.labels,
                                                     str(runner), target.work_directory, target.unit, target.shared_key)

            def execute(config, args, until, input=None):
                self.assertEqual(args[:10], ['shell', '--tty=false', profile['vm'], '--', 'sudo', '-iu', 'ci', 'bash', '-s', '--'])
                self.assertEqual(args[10:], [str(os.getuid()), str(runner)])
                self.assertIn('files=(.runner .credentials .credentials_rsaparams)', input)
                result = subprocess.run(['bash', '-s', '--', *args[10:]],
                                        input=input, text=True, capture_output=True,
                                        env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']}, timeout=5)
                if result.returncode:
                    raise cli.Failure('synthetic pre-registration boundary refused')
                return result.stdout

            with patch.object(cli, 'lima', side_effect=execute):
                self.assertIsNone(cli.local_registration(profile, observed_target, cli.deadline(1)))

    def test_local_registration_accepts_one_leading_bom_and_refuses_other_bom_positions(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        target = cli.registration_target(profile)
        value = {'agentId': 41, 'agentName': target.name, 'gitHubUrl': target.url,
                 'workFolder': target.work_directory}
        encoded = json.dumps(value)
        with patch.object(cli, 'lima', return_value='Present\n\ufeff' + encoded):
            local = cli.local_registration(profile, target, cli.deadline(1))
        self.assertEqual(local.runner_id, 41)
        malformed = ('Present\n\ufeff\ufeff' + encoded,
                     'Present\n' + encoded.replace('"agentId": 41', '"agentId": \ufeff41'))
        for output in malformed:
            with self.subTest(output=output), patch.object(cli, 'lima', return_value=output):
                with self.assertRaises(cli.Failure):
                    cli.local_registration(profile, target, cli.deadline(1))

    def test_registration_refuses_online_runner_and_post_enable_drift(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        target, local, remote = self.registration_evidence(profile)
        online = cli.RemoteRegistration(remote.runner_id, remote.name, 'online', False, remote.labels)
        common = dict(github_auth=patch.object(cli, 'github_auth'), state=patch.object(cli, 'vm_state', return_value='Running'),
                      packages=patch.object(cli, 'ensure_no_package_work'), contract=patch.object(cli, 'group_contract'),
                      idle=patch.object(cli, 'group_idle', return_value=True), local=patch.object(cli, 'local_registration', return_value=local))
        with common['github_auth'], common['state'], common['packages'], common['contract'], common['idle'], common['local'], \
                patch.object(cli, 'github_runners', return_value=(online,)), patch.object(cli, 'persist_runner_id') as persist:
            with self.assertRaises(cli.Failure):
                cli.register_runner(cli.Selection(profile, (profile,)), cli.deadline(1))
        persist.assert_not_called()

        with patch.object(cli, 'github_auth'), patch.object(cli, 'vm_state', return_value='Running'), \
                patch.object(cli, 'ensure_no_package_work'), patch.object(cli, 'group_contract'), \
                patch.object(cli, 'group_idle', return_value=True), patch.object(cli, 'local_registration', return_value=local), \
                patch.object(cli, 'github_runners', side_effect=[(remote,), (online,)]), patch.object(cli, 'persist_runner_id'), \
                patch.object(cli, 'enable_registration_unit'), patch.object(cli, 'finish_shared_registration') as finish:
            with self.assertRaises(cli.Failure) as failure:
                cli.register_runner(cli.Selection(profile, (profile,)), cli.deadline(1))
        self.assertIn('changed after service enablement', str(failure.exception))
        finish.assert_not_called()

    def test_register_default_timeout_is_ten_minutes_and_explicit_value_wins(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        for argv, expected in ((['--repo', 'owner/chat', 'register'], 600),
                               (['--repo', 'owner/chat', 'register', '--timeout', '17'], 17.0)):
            with self.subTest(argv=argv), patch.object(cli, 'load_config', return_value=profile), \
                    patch.object(cli, 'register_runner') as register, patch.object(cli, 'operation_lock'), \
                    patch.object(cli, 'deadline', side_effect=lambda seconds: seconds):
                self.assertEqual(cli.main(argv), 0)
            self.assertEqual(register.call_args.args[1], expected)

    def test_finish_shared_registration_stages_both_files_and_cleans_only_after_success(self):
        selection = self.shared_selection()
        target = cli.registration_target(selection)
        stage = '/tmp/ci-vm-register.ABC123'
        with patch.object(cli, 'lima', side_effect=[stage + '\n', '', '', '', '', '']) as lima:
            cli.finish_shared_registration(selection, target, cli.deadline(1))
        calls = [call.args[1] for call in lima.call_args_list]
        self.assertEqual(calls[1][0], 'copy')
        self.assertTrue(calls[1][1].endswith('prepare-shared-runner.sh'))
        self.assertEqual(calls[2][0], 'copy')
        self.assertTrue(calls[2][1].endswith('ci-vm-runner@.service'))
        self.assertEqual(calls[3][0], 'copy')
        self.assertTrue(calls[3][1].endswith('container-runtime-state.sh'))
        self.assertEqual(calls[4][-3:], ['finish', target.shared_key, '--registration-ready'])
        self.assertEqual(calls[5], ['shell', selection['vm'], '--', 'rm', '-rf', '--', stage])

        with patch.object(cli, 'lima', side_effect=[stage + '\n', '', '', '', cli.Failure('finish failed')]) as lima:
            with self.assertRaises(cli.Failure):
                cli.finish_shared_registration(selection, target, cli.deadline(1))
        self.assertFalse(any(call.args[1][:4] == ['shell', selection['vm'], '--', 'rm'] for call in lima.call_args_list))

    def test_registration_auth_token_and_pagination_boundaries_fail_closed(self):
        profile = dict(self.config, version=2, repo='owner/chat')
        target = cli.registration_target(profile)
        with patch.object(cli.shutil, 'which', return_value=None), patch.object(cli, 'run') as run:
            with self.assertRaises(cli.Failure) as failure:
                cli.github_auth(cli.deadline(1))
        self.assertIn('GitHub CLI is required', str(failure.exception))
        run.assert_not_called()
        with patch.object(cli.shutil, 'which', return_value='/usr/bin/gh'), \
                patch.object(cli, 'run', side_effect=cli.Failure('synthetic raw auth response')):
            with self.assertRaises(cli.Failure) as failure:
                cli.github_auth(cli.deadline(1))
        self.assertIn('gh auth login', str(failure.exception))
        self.assertNotIn('synthetic raw', str(failure.exception))
        timeout = cli.Failure('Host command timed out.', 4)
        with patch.object(cli.shutil, 'which', return_value='/usr/bin/gh'), patch.object(cli, 'run', side_effect=timeout):
            with self.assertRaises(cli.Failure) as failure:
                cli.github_auth(cli.deadline(1))
        self.assertIs(failure.exception, timeout)
        with patch.object(cli, 'run', side_effect=timeout):
            with self.assertRaises(cli.Failure) as failure:
                cli.registration_token(target, cli.deadline(1))
        self.assertIs(failure.exception, timeout)

        for response in ('{}', json.dumps({'token': 'x' * 30, 'expires_at': '2000-01-01T00:00:00Z'})):
            with self.subTest(response=response), patch.object(cli, 'run', return_value=response):
                with self.assertRaises(cli.Failure):
                    cli.registration_token(target, cli.deadline(1))

        first = {'id': 1, 'name': 'other', 'status': 'offline', 'busy': False, 'labels': []}
        second = {'id': 2, 'name': target.name, 'status': 'offline', 'busy': False,
                  'labels': [{'name': value} for value in ('self-hosted', 'Linux', 'ARM64', *target.labels)]}
        pages = json.dumps([{'total_count': 2, 'runners': [first]}, {'total_count': 2, 'runners': [second]}])
        with patch.object(cli, 'run', return_value=pages) as run:
            runners = cli.github_runners(target, cli.deadline(1))
        self.assertEqual([runner.runner_id for runner in runners], [1, 2])
        self.assertIn('--paginate', run.call_args.args[0])
        self.assertIn('--slurp', run.call_args.args[0])

    def test_setup_preserves_adopted_services_and_rejects_another_repository(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            cli.setup_guide(dict(self.config, guest_user='builder', guest_uid=501, unit='custom.service'), 'owner/repo')
        text = output.getvalue()
        self.assertIn('user builder | UID 501 | unit custom.service', text)
        self.assertIn('Keep its existing registration and service.', text)
        self.assertIn('maintenance.md#adopt-without-changing-existing-behavior', text)
        self.assertNotIn('ci-vm resume', text)
        self.assertNotIn('sudo -iu', text)
        self.assertNotIn('config.sh', text)
        self.assertNotIn('settings/actions/runners/new', text)
        with self.assertRaises(cli.Failure) as result:
            cli.setup_guide(dict(self.config, repo='other/repo', runner_id=42), 'owner/repo')
        self.assertEqual(result.exception.code, 2)

    def test_setup_missing_or_invalid_configuration_does_not_probe_or_create_files(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            config_path = home / '.config/github-runner-vm/config.json'
            with patch.dict(os.environ, HOME=name), patch.object(cli, 'run') as run:
                with self.assertRaises(cli.Failure) as result:
                    cli.main(['setup', 'owner/repo'])
                self.assertIn('No installed VM configuration', str(result.exception))
                self.assertIn(str(ROOT / 'docs/setup.md'), str(result.exception))
                self.assertNotIn('github.com/builtbycalvin/github-runner-vm/blob/', str(result.exception))
                self.assertEqual(list(home.iterdir()), [])
                config_path.parent.mkdir(parents=True)
                config_path.write_text('{"unexpected": true}')
                config_path.chmod(0o600)
                with self.assertRaises(cli.Failure):
                    cli.main(['setup', 'owner/repo'])
                self.assertEqual(config_path.read_text(), '{"unexpected": true}')
                run.assert_not_called()

    def test_terminal_color_respects_no_color_and_dumb_terminal(self):
        for isatty, term, no_color, expected in ((True, 'xterm', False, True), (False, 'xterm', False, False), (True, 'dumb', False, False), (True, 'xterm', True, False)):
            output = io.StringIO()
            output.isatty = lambda: isatty
            env = {'TERM': term}
            if no_color:
                env['NO_COLOR'] = ''
            with self.subTest(isatty=isatty, term=term, no_color=no_color), patch.dict(os.environ, env, clear=True), patch('sys.stdout', output):
                cli.overview()
            self.assertEqual('\033[' in output.getvalue(), expected)

    def test_config_rejects_injection_and_unpaired_identity(self):
        for update in ({'vm': 'ci;true'}, {'vm': '.'}, {'vm': '..'}, {'unit': '../bad'}, {'repo': 'owner/repo'}, {'guest_uid': True}, {'lima_home': 'relative'}):
            with self.subTest(update=update), self.assertRaises(cli.Failure):
                cli.validate_config(dict(self.config, **update))

    def test_creation_resources_use_native_flags_and_adoption_preserves_them(self):
        template = (ROOT / 'config/lima.yaml').read_bytes()
        for options, expected in (([], dict(cpus=2, memory_gib=2, disk_gib=20)),
                                  (['--cpus', '1', '--memory', '1', '--disk', '12'], dict(cpus=1, memory_gib=1, disk_gib=12)),
                                  (['--cpus', '4', '--memory', '8', '--disk', '60'], dict(cpus=4, memory_gib=8, disk_gib=60))):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as name:
                home = Path(name)
                lima_home = '/tmp/ci-vm-' + hashlib.sha256(name.encode()).hexdigest()[:12]
                calls = []
                def lima(config, args, until, input=None):
                    calls.append((dict(config), args))
                    return '[]' if args[0] == 'list' else 'limactl version 2.2.0' if args[0] == '--version' else ''
                with patch.dict(os.environ, HOME=name), patch.object(cli, 'host_architecture', return_value=SUPPORTED_DARWIN_ARM64), patch.object(cli, 'lima', side_effect=lima), patch('sys.stdout', io.StringIO()):
                    self.assertEqual(cli.main(['--install', '--repo', 'Owner/Repo', '--provision', '--yes-create-vm', '--lima-home', lima_home, *options]), 0)
                    profile = home / '.config/github-runner-vm/profiles' / (cli.profile_key('owner/repo') + '.json')
                    config = json.loads(profile.read_text())
                    self.assertEqual(config['repo'], 'owner/repo')
                    self.assertEqual(config['resources'], expected)
                    self.assertEqual(config['vm'], cli.automatic_vm_name('owner/repo', config['lima_home']))
                    self.assertEqual(calls[-1][1], ['start', '--tty=false', '--name', config['vm'], '--cpus', str(expected['cpus']), '--memory', str(expected['memory_gib']), '--disk', str(expected['disk_gib']), str(home / '.local/share/github-runner-vm/config/lima.yaml')])
                    installed_template = home / '.local/share/github-runner-vm/config/lima.yaml'
                    self.assertEqual(installed_template.read_bytes(), template)
                    original = (profile.read_bytes(), profile.stat().st_mtime_ns)
                    with patch.object(cli, 'vm_state', return_value='Stopped'):
                        count = len(calls)
                        self.assertEqual(cli.main(['--install', '--repo', 'owner/repo', '--adopt', config['vm']]), 0)
                        self.assertEqual(len(calls), count)
                        for update in (['--memory', '8'], ['--cpus', '4'], ['--disk', '60']):
                            with self.assertRaises(cli.Failure):
                                cli.main(['--install', '--repo', 'owner/repo', '--adopt', config['vm'], *update])
                        with self.assertRaises(cli.Failure):
                            cli.main(['--install', '--repo', 'owner/repo', '--provision', '--yes-create-vm', *options])
                    self.assertEqual((profile.read_bytes(), profile.stat().st_mtime_ns), original)
                    self.assertFalse((profile.parent.parent / 'config.json').exists())

    def test_invalid_resource_requests_refuse_before_any_vm_probe_or_write(self):
        for args in (['--cpus', '0'], ['--cpus', '65'], ['--memory', '0'], ['--memory', '513'], ['--disk', '7'], ['--disk', '4097']):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as name, patch.dict(os.environ, HOME=name), patch.object(cli, 'vm_state') as state:
                with self.assertRaises(cli.Failure):
                    cli.main(['--install', '--repo', 'owner/repo', '--provision', '--yes-create-vm', *args])
                state.assert_not_called()
                self.assertEqual(list(Path(name).iterdir()), [])
        profile = dict(self.config, version=2, repo='owner/repo')
        for resources in ({'cpus': True, 'memory_gib': 2, 'disk_gib': 20}, {'cpus': 2, 'memory_gib': 2}, {'cpus': 2, 'memory_gib': 2, 'disk_gib': 20, 'mounts': []}):
            with self.subTest(resources=resources), self.assertRaises(cli.Failure):
                cli.validate_config(dict(profile, resources=resources))

    def test_creation_timeout_reserves_profile_and_never_recreates(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, HOME=name), patch.object(cli, 'host_architecture', return_value=SUPPORTED_DARWIN_ARM64), patch('sys.stdout', io.StringIO()):
            lima_home = '/tmp/ci-vm-' + hashlib.sha256(name.encode()).hexdigest()[:12]
            def lima(config, args, until, input=None):
                if args[0] == 'start':
                    raise cli.Failure('Host command timed out.', 4)
                return '[]' if args[0] == 'list' else 'limactl version 2.2.0'
            with patch.object(cli, 'lima', side_effect=lima):
                with self.assertRaises(cli.Failure) as failure:
                    cli.main(['--install', '--repo', 'owner/repo', '--provision', '--yes-create-vm', '--lima-home', lima_home])
                self.assertEqual(failure.exception.code, 4)
            profile = Path(name) / '.config/github-runner-vm/profiles' / (cli.profile_key('owner/repo') + '.json')
            original = profile.read_bytes()
            with patch.object(cli, 'vm_state', return_value='Absent'), patch.object(cli, 'lima') as operation:
                with self.assertRaises(cli.Failure):
                    cli.main(['--install', '--repo', 'owner/repo', '--provision', '--yes-create-vm'])
                operation.assert_not_called()
            self.assertEqual(profile.read_bytes(), original)

    def test_definite_provision_failure_removes_only_unchanged_absent_reservation(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, HOME=name):
            home = Path(name)
            config = dict(self.config, version=2, repo='owner/repo', lima_home=str(home / 'lima'))
            profile = home / '.config/github-runner-vm/profiles' / (cli.profile_key(config['repo']) + '.json')
            profile.parent.mkdir(parents=True, mode=0o700)
            written = (json.dumps(config, indent=2) + '\n').encode()
            cli.atomic_write(profile, written, 0o600)
            with patch.object(cli, 'vm_state', return_value='Absent'):
                self.assertTrue(cli.cleanup_failed_provision_reservation(config, (profile, written), cli.deadline(1)))
            self.assertFalse(profile.exists())
            cli.atomic_write(profile, b'changed\n', 0o600)
            with patch.object(cli, 'vm_state', return_value='Absent'):
                self.assertFalse(cli.cleanup_failed_provision_reservation(config, (profile, written), cli.deadline(1)))
            self.assertEqual(profile.read_bytes(), b'changed\n')
            instance = Path(config['lima_home']) / config['vm']
            instance.parent.mkdir()
            instance.symlink_to(instance.parent / 'missing')
            cli.atomic_write(profile, written, 0o600)
            with patch.object(cli, 'vm_state', return_value='Absent'):
                self.assertFalse(cli.cleanup_failed_provision_reservation(config, (profile, written), cli.deadline(1)))
            self.assertTrue(profile.exists())

    def test_provisioning_cleans_fresh_reservation_after_definite_start_or_second_preflight_failure(self):
        for failure_at_start in (True, False):
            with self.subTest(failure_at_start=failure_at_start), tempfile.TemporaryDirectory() as name, \
                    patch.dict(os.environ, HOME=name), patch.object(cli, 'host_architecture', return_value=SUPPORTED_DARWIN_ARM64), \
                    patch('sys.stdout', io.StringIO()):
                lima_home = '/tmp/ci-vm-' + hashlib.sha256(name.encode()).hexdigest()[:12]
                def lima(config, args, until, input=None):
                    if args[0] == 'list':
                        return '[]'
                    if args[0] == '--version':
                        return 'limactl version 2.2.0'
                    if args[0] == 'start':
                        raise cli.Failure('start failed', 3)
                    raise AssertionError(args)
                preflight = [None, None if failure_at_start else cli.Failure('inherited config', 3)]
                with patch.object(cli, 'lima', side_effect=lima), patch.object(cli, 'reject_inherited_config', side_effect=preflight):
                    with self.assertRaises(cli.Failure) as failure:
                        cli.main(['--install', '--repo', 'owner/repo', '--provision', '--yes-create-vm', '--lima-home', lima_home])
                self.assertEqual(failure.exception.code, 3)
                self.assertEqual(str(failure.exception), 'start failed' if failure_at_start else 'inherited config')
                profile = Path(name) / '.config/github-runner-vm/profiles' / (cli.profile_key('owner/repo') + '.json')
                self.assertFalse(profile.exists())

    def test_selected_repository_reaches_maintenance_and_never_needs_runner_id(self):
        profile = dict(self.config, version=2, repo='owner/repo', vm='repo-vm')
        with patch.object(cli, 'configurations', return_value=[(Path('/tmp/profile.json'), profile)]), patch.object(cli, 'maintain') as maintain, patch.object(cli, 'operation_lock'):
            for args in (['--repo', 'Owner/Repo', 'pause'], ['resume', '--repo', 'owner/repo'], ['restart', '--repo', 'owner/repo']):
                self.assertEqual(cli.main(args), 0)
                self.assertEqual(dict(maintain.call_args.args[0].selected), profile)
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'guest', return_value='ActiveState=inactive\n'), patch.object(cli, 'run') as run, patch('sys.stdout', io.StringIO()):
            cli.report(profile, 'status', cli.deadline(1))
            run.assert_not_called()

    def test_profile_names_are_bounded_and_distinguish_similar_repositories(self):
        first = cli.profile_key(cli.repository_value('https://github.com/OWNER/Repo.git/'))
        self.assertEqual(first, cli.profile_key('owner/repo'))
        self.assertNotEqual(cli.profile_key('owner/a-b'), cli.profile_key('owner-a/b'))
        key = cli.profile_key('o' * 100 + '/' + 'r' * 100)
        self.assertLessEqual(len('ci-' + key), 63)
        self.assertTrue(cli.IDENTIFIER.fullmatch('ci-' + key))
        repo = 'builtbycalvin/github-runner-vm'
        lima_home = '/Users/localai/.local/share/github-runner-vm/lima'
        vm = cli.automatic_vm_name(repo, lima_home)
        self.assertEqual(vm, cli.automatic_vm_name(repo, lima_home))
        self.assertNotEqual(vm, cli.automatic_vm_name(repo + '-other', lima_home))
        cli.validate_lima_socket_path(lima_home, vm)
        instance_path = len(os.fsencode(str(Path(lima_home) / vm)))
        self.assertLessEqual(instance_path + cli.LIMA_SOCKET_COMPONENT_RESERVE + 1, cli.DARWIN_UNIX_PATH_BYTES)

    def test_provisioning_refuses_oversized_socket_path_before_profile_write(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, HOME=name), patch.object(cli, 'host_architecture', return_value=SUPPORTED_DARWIN_ARM64), patch.object(cli, 'lima', return_value='[]') as lima:
            with self.assertRaises(cli.Failure) as failure:
                cli.main(['--install', '--repo', 'owner/repo', '--provision', 'x' * 80, '--yes-create-vm'])
            self.assertIn('Unix socket path limit', str(failure.exception))
            self.assertFalse((Path(name) / '.config').exists())
            self.assertEqual(lima.call_count, 1)
            self.assertEqual(lima.call_args.args[1], ['list', '--json'])
        with self.assertRaises(cli.Failure) as failure:
            cli.automatic_vm_name('owner/repo', '/Users/' + 'x' * 90)
        self.assertIn('Lima home is too long', str(failure.exception))

    def package_plan(self, installed=None, version='14.1.0-1', changed=True):
        output = f'P\tripgrep\t{installed or "-"}\nR\tno\nPLAN\n'
        output += f'0 upgraded, {1 if changed else 0} newly installed, 0 to remove and 85 not upgraded.\n'
        if changed:
            output += f'Inst ripgrep ({version} Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [arm64])\nConf ripgrep ({version} Ubuntu:24.04/noble [arm64])\n'
        return cli.package_observation(output, [cli.package_request('ripgrep')])

    def package_command(self, flags, plans=None, apt_error=None, idle=True, contract_error=None, answer=None):
        events = []
        state = {'gate': False}
        planned = self.package_plan()
        completed = self.package_plan(installed='14.1.0-1', changed=False)
        observations = iter(plans if plans is not None else [planned, planned, planned, completed])
        def probe(*args):
            events.append('probe')
            value = next(observations)
            if isinstance(value, Exception):
                raise value
            return value
        def guest(config, script, until, *args):
            if 'echo PackageGate=' in script:
                return 'PackageGate=active\n' if state['gate'] else 'PackageGate=clear\n'
            if 'mkdir -m 700' in script:
                events.append('gate-created')
                state['gate'] = True
                return 'PackageGate=0:700\n'
            if 'rmdir --' in script:
                events.append('gate-cleared')
                state['gate'] = False
                return ''
            self.assertIn('apt-get', script)
            self.assertIn('--no-remove', script)
            self.assertIn('allow-downgrades=false', script)
            self.assertIn('AllowUnauthenticated=false', script)
            self.assertIn('--no-install-recommends', script)
            self.assertEqual(args, ('ripgrep=14.1.0-1',))
            events.append('apt')
            if apt_error:
                raise apt_error
            return ''
        stdin = io.StringIO(answer or '')
        stdin.isatty = lambda: answer is not None
        output = io.StringIO()
        with patch.object(cli, 'load_config', return_value=cli.Selection(dict(self.config, version=2, repo='owner/repo'), (dict(self.config, version=2, repo='owner/repo'),))), patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract', side_effect=contract_error), patch.object(cli, 'idle', return_value=idle), patch.object(cli, 'package_probe', side_effect=probe), patch.object(cli, 'guest', side_effect=guest), patch.object(cli, 'operation_lock'), patch('sys.stdin', stdin), patch('sys.stdout', output), patch('sys.stderr', io.StringIO()):
            code = cli.main(['--repo', 'owner/repo', 'packages', 'ripgrep', '--json', *flags])
        return code, json.loads(output.getvalue()), events, state

    def test_package_requests_and_simulation_are_exact_and_bounded(self):
        self.assertEqual(cli.package_request('libpq-dev=16.15-0ubuntu0.24.04.1').name, 'libpq-dev')
        for value in ('-y', 'foo-', 'foo+', 'foo*', 'foo[bar]', '/tmp/foo.deb', 'https://host/pkg', 'foo;id', 'foo:amd64', 'foo=1;id', 'foo='):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cli.package_request(value)
        self.assertEqual(self.package_plan()['changes'][0]['version'], '14.1.0-1')
        for suffix in ('Remv ripgrep [1]\n', 'Inst ripgrep ???\n', 'W: stale package indexes\n'):
            with self.assertRaises(ValueError):
                cli.package_observation('P\tripgrep\t1\nR\tno\nPLAN\n' + suffix, [cli.package_request('ripgrep')])
        protected = 'P\tdocker-ce\t1\nR\tno\nPLAN\nInst docker-ce [1] (2 repo [arm64])\nConf docker-ce (2 repo [arm64])\n'
        with self.assertRaises(ValueError):
            cli.package_observation(protected, [cli.package_request('docker-ce')])

        with self.assertRaises(ValueError):
            cli.package_observation('P\tripgrep\t1\nR\tno\nPLAN\n', [cli.package_request('ripgrep')])
        stderr = io.StringIO()
        with patch('sys.stderr', stderr), self.assertRaises(SystemExit):
            cli.main(['packages', 'https://example.invalid/pkg?token=SENTINEL'])
        self.assertNotIn('SENTINEL', stderr.getvalue())

    def test_package_preview_and_confirmation_never_mutate(self):
        code, receipt, events, state = self.package_command([])
        self.assertEqual(code, 0)
        self.assertEqual(receipt['outcome'], 'preview')
        self.assertEqual(events, ['probe'])
        self.assertFalse(state['gate'])
        for flags, answer in ((['--yes'], None), (['--apply'], None), (['--apply'], 'no\n')):
            code, receipt, events, state = self.package_command(flags, answer=answer)
            self.assertEqual(code, 2)
            self.assertNotIn('apt', events)
            self.assertFalse(state['gate'])

    def test_package_apply_verifies_versions_then_clears_gate_and_leaves_paused(self):
        for flags, answer in ((['--apply', '--yes'], None), (['--apply'], 'yes\n')):
            code, receipt, events, state = self.package_command(flags, answer=answer)
            self.assertEqual(code, 0, receipt)
            self.assertEqual(receipt['outcome'], 'installed')
            self.assertTrue(receipt['paused'])
            self.assertEqual(receipt['installed'], {'ripgrep': '14.1.0-1'})
            self.assertLess(events.index('gate-created'), events.index('apt'))
            self.assertEqual(events[-2:], ['probe', 'gate-cleared'])
            self.assertFalse(state['gate'])
        noop = self.package_plan(installed='14.1.0-1', changed=False)
        code, receipt, events, state = self.package_command(['--apply', '--yes'], plans=[noop] * 4)
        self.assertEqual(code, 0, receipt)
        self.assertNotIn('apt', events)
        self.assertNotIn('gate-created', events)

    def test_package_busy_contract_and_transaction_drift_refuse(self):
        for kwargs in ({'idle': False}, {'contract_error': cli.Failure('contract differs', 3)},
                       {'plans': [self.package_plan(), self.package_plan(version='15')]}):
            code, receipt, events, state = self.package_command(['--apply', '--yes'], **kwargs)
            self.assertNotEqual(code, 0)
            self.assertNotIn('apt', events)
            self.assertFalse(state['gate'])

    def test_package_failures_keep_gate_and_block_resume_restart(self):
        planned = self.package_plan()
        for kwargs in ({'apt_error': cli.Failure('raw APT secret output', 1)}, {'apt_error': cli.Failure('timeout', 4)},
                       {'plans': [planned, planned, planned, ValueError('post-install readback failed')]},
                       {'plans': [planned, planned, planned, self.package_plan(installed='wrong', changed=False)]}):
            code, receipt, events, state = self.package_command(['--apply', '--yes'], **kwargs)
            self.assertNotEqual(code, 0)
            self.assertTrue(state['gate'])
            self.assertEqual(receipt['paused'], 'unverified')
            self.assertNotIn('raw APT secret output', json.dumps(receipt))
            self.assertNotIn('gate-cleared', events)
            for command in ('resume', 'restart'):
                with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'package_gate', return_value=state['gate']), patch.object(cli, 'idle', return_value=True), patch.object(cli, 'guest') as guest, patch.object(cli, 'lima') as lima:
                    with self.assertRaises(cli.Failure):
                        cli.maintain(cli.Selection(self.config, (self.config,)), command, cli.deadline(1))
                    guest.assert_not_called()
                    lima.assert_not_called()

    def test_package_default_and_explicit_deadlines(self):
        for args, expected in ((['packages', 'ripgrep'], 600), (['--timeout', '12', 'packages', 'ripgrep'], 12), (['packages', 'ripgrep', '--timeout', '34'], 34)):
            with patch.object(cli, 'packages', return_value=0) as operation:
                self.assertEqual(cli.main(args), 0)
                self.assertEqual(operation.call_args.args[0].timeout, expected)

    def actions_evidence(self):
        run = dict(id=101, repository={'full_name': 'owner/repo'}, head_sha='a' * 40, event='workflow_dispatch', run_attempt=2, status='completed', conclusion='success')
        job = dict(id=201, name='target', run_id=101, head_sha='a' * 40, run_attempt=2, status='completed', conclusion='success', runner_id=7, runner_name='repo-runner', labels=['self-hosted', 'Linux', 'ARM64'])
        return run, job

    def verify_command(self, responses, extra=(), runner_id=7):
        profile = dict(self.config, version=2, repo='owner/repo')
        if runner_id is not None:
            profile['runner_id'] = runner_id
        output = io.StringIO()
        with patch.object(cli, 'load_config', return_value=profile), patch.object(cli, 'run', side_effect=[json.dumps(response) for response in responses]) as run, patch.object(cli, 'guest') as guest, patch.object(cli, 'vm_state') as vm, patch('sys.stdout', output):
            code = cli.main(['--repo', 'owner/repo', 'verify-run', '101', '--expect-sha', 'a' * 40, '--expect-event', 'workflow_dispatch', '--expect-runner-id', '7', '--job', 'target', '--json', *extra])
            guest.assert_not_called()
            vm.assert_not_called()
        return code, json.loads(output.getvalue()), run.call_args_list

    def test_verify_run_checks_attempt_pages_and_allows_other_hosted_jobs(self):
        run, target = self.actions_evidence()
        others = [dict(target, id=300 + index, name=f'hosted-{index}', runner_id=100, runner_name='GitHub Actions', labels=['ubuntu-latest']) for index in range(100)]
        code, receipt, calls = self.verify_command([run, {'total_count': 101, 'jobs': [target, *others[:99]]}, {'total_count': 101, 'jobs': others[99:]}, run])
        self.assertEqual(code, 0, receipt)
        self.assertTrue(receipt['verified'])
        self.assertEqual(receipt['attempt'], 2)
        self.assertEqual(len(receipt['jobs']), 1)
        self.assertIn('/attempts/2/jobs?per_page=100&page=1', calls[1].args[0][-1])
        self.assertIn('/attempts/2/jobs?per_page=100&page=2', calls[2].args[0][-1])
        self.assertEqual(calls[0].args[0], calls[-1].args[0])

    def test_verify_run_refuses_missing_duplicate_pending_failed_or_wrong_jobs(self):
        run, job = self.actions_evidence()
        for jobs in ([], [dict(job, name='other')], [job, dict(job, id=202)], [dict(job, status='queued', conclusion=None)],
                     [dict(job, conclusion='skipped')], [dict(job, conclusion='failure')], [dict(job, runner_id=8)],
                     [dict(job, runner_name='')], [dict(job, labels=['self-hosted', 'Linux', 'X64'])]):
            with self.subTest(jobs=jobs):
                code, receipt, _ = self.verify_command([run, {'total_count': len(jobs), 'jobs': jobs}, run])
                self.assertNotEqual(code, 0)
                self.assertNotEqual(receipt['outcome'], 'verified')

    def test_verify_run_refuses_unlisted_job_on_selected_runner_and_invalid_runner_id(self):
        run, job = self.actions_evidence()
        unexpected = dict(job, id=202, name='unlisted')
        code, receipt, _ = self.verify_command([run, {'total_count': 2, 'jobs': [job, unexpected]}, run])
        self.assertEqual(code, 3)
        self.assertIn('Unexpected job ran on intended runner: unlisted', receipt['issues'])
        for runner_id in (0, -1, True, '7'):
            with self.subTest(runner_id=runner_id):
                code, receipt, _ = self.verify_command([run, {'total_count': 1, 'jobs': [dict(job, runner_id=runner_id)]}, run])
                self.assertEqual(code, 3)
                self.assertIn('error', receipt)

    def test_verify_run_rejects_malformed_identities_and_run_fields(self):
        run, job = self.actions_evidence()
        for update in ({'run_id': True}, {'run_attempt': 1}, {'run_attempt': True}, {'head_sha': 'b' * 40}, {'status': []}, {'conclusion': {}}, {'runner_name': []}, {'labels': [7]}):
            with self.subTest(update=update):
                code, receipt, _ = self.verify_command([run, {'total_count': 1, 'jobs': [dict(job, **update)]}, run])
                self.assertNotEqual(code, 0)
                self.assertIn('error', receipt)
        for update in ({'id': True}, {'repository': {'full_name': 'wrong/repo'}}, {'head_sha': 'b' * 40}, {'event': 'push'}, {'run_attempt': True}, {'status': []}, {'conclusion': {}}):
            with self.subTest(update=update):
                code, receipt, calls = self.verify_command([dict(run, **update)])
                self.assertNotEqual(code, 0)
                self.assertEqual(len(calls), 1)

    def test_verify_run_rejects_incomplete_pages_and_concurrent_rerun(self):
        run, job = self.actions_evidence()
        for responses in ([run, {'total_count': 2, 'jobs': [job]}],
                          [run, {'total_count': 1, 'jobs': []}],
                          [run, {'total_count': True, 'jobs': [job]}],
                          [run, {'total_count': 1, 'jobs': [job]}, dict(run, run_attempt=3)],
                          [dict(run, status='in_progress', conclusion=None), {'total_count': 1, 'jobs': [job]}, dict(run, status='in_progress', conclusion=None)]):
            code, receipt, _ = self.verify_command(responses)
            self.assertNotEqual(code, 0)
            self.assertNotEqual(receipt['outcome'], 'verified')

    def test_verify_run_rejects_conflicting_profile_id_and_invalid_expectations_without_gh(self):
        code, receipt, calls = self.verify_command([], runner_id=None)
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        code, receipt, calls = self.verify_command([], runner_id=8)
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        for extra in (['--job', 'target'], ['--job', ''], ['--expect-sha', 'short'], ['--expect-event', 'push;id']):
            code, receipt, calls = self.verify_command([], extra=extra)
            self.assertEqual(code, 2)
            self.assertEqual(calls, [])

    def test_repeated_or_conflicting_selectors_refuse_before_reading_state(self):
        for args in (['--repo', 'owner/one', 'status', '--repo', 'owner/two'],
                     ['--repo=owner/one', 'status', '--repo=owner/one'],
                     ['--legacy', 'status', '--legacy'],
                     ['--legacy', 'status', '--repo', 'owner/one'],
                     ['--repo', 'owner/one', 'setup', 'owner/two']):
            with self.subTest(args=args), patch.object(cli, 'configurations') as configs, patch.object(cli, 'run') as run, patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    cli.main(args)
                self.assertEqual(failure.exception.code, 2)
                configs.assert_not_called()
                run.assert_not_called()

    def test_abbreviated_selectors_refuse_before_reading_or_changing_state(self):
        for args in (['--rep', 'owner/one', 'pause', '--repo', 'owner/two'],
                     ['--repo', 'owner/one', 'pause', '--rep', 'owner/two'],
                     ['status', '--rep', 'owner/one'],
                     ['--leg', 'status'], ['pause', '--leg']):
            with self.subTest(args=args), patch.object(cli, 'configurations') as configs, patch.object(cli, 'maintain') as maintain, patch.object(cli, 'report') as report, patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    cli.main(args)
                self.assertEqual(failure.exception.code, 2)
                configs.assert_not_called()
                maintain.assert_not_called()
                report.assert_not_called()

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
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'guest', side_effect=lambda *a, **k: calls.append(a) or ''), patch.object(cli, 'idle', return_value=False):
            with self.assertRaises(cli.Failure) as result:
                cli.maintain(cli.Selection(self.config, (self.config,)), 'pause', cli.deadline(0.03))
        self.assertEqual(result.exception.code, 4)
        self.assertFalse(any('stop' in str(call) or 'kill' in str(call) for call in calls))

    def test_incompatible_refuses_before_mutation(self):
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract', side_effect=cli.Failure('incompatible', 3)), patch.object(cli, 'guest') as guest:
            with self.assertRaises(cli.Failure):
                cli.maintain(cli.Selection(self.config, (self.config,)), 'pause', cli.deadline(1))
            guest.assert_not_called()

    def test_restart_requires_paused_idle(self):
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'package_gate', return_value=False), patch.object(cli, 'idle', return_value=False), patch.object(cli, 'lima') as lima:
            with self.assertRaises(cli.Failure):
                cli.maintain(cli.Selection(self.config, (self.config,)), 'restart', cli.deadline(1))
            lima.assert_not_called()

    def idle_output(self, **changes):
        state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', ControlGroup='', Job='0', Paused='yes', Runners='0', Containers='0', RuntimeDrift='no', Jobs='0', CgroupEmpty='yes')
        state.update(changes)
        return ''.join(f'{key}={value}\n' for key, value in state.items())

    def test_each_restart_guard_is_required(self):
        for change in ({'ActiveState': 'activating'}, {'SubState': 'auto-restart'}, {'MainPID': '5'}, {'ControlPID': '5'}, {'Job': '7'}, {'Paused': 'no'}, {'Runners': '1'}, {'Containers': '1'}, {'RuntimeDrift': 'yes'}, {'Jobs': '1'}, {'CgroupEmpty': 'no'}):
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

    def test_rosetta_process_is_recognized_as_apple_silicon(self):
        with patch.object(cli.platform, 'system', return_value='Darwin'), patch.object(cli.platform, 'machine', return_value='x86_64'), \
                patch.object(cli, 'run', return_value='1\n') as run:
            host = cli.host_architecture(cli.deadline(1))
        self.assertTrue(host.supported)
        self.assertTrue(host.translated)
        self.assertEqual(run.call_args.args[0], ['/usr/sbin/sysctl', '-in', 'sysctl.proc_translated'])

    def test_host_architecture_sysctl_failures_are_inconclusive(self):
        with patch.object(cli.platform, 'system', return_value='Darwin'), patch.object(cli.platform, 'machine', return_value='arm64'), \
                patch.object(cli, 'run', side_effect=cli.Failure('sysctl failed')):
            with self.assertRaises(cli.Failure) as failure:
                cli.host_architecture(cli.deadline(1))
            self.assertEqual(failure.exception.code, 3)
            self.assertIn('inconclusive', str(failure.exception))
        with patch.object(cli.platform, 'system', return_value='Darwin'), patch.object(cli.platform, 'machine', return_value='x86_64'), \
                patch.object(cli, 'run', side_effect=['1\n', cli.Failure('sysctl failed')]):
            with self.assertRaises(cli.Failure) as failure:
                cli.host_architecture(cli.deadline(1))
            self.assertEqual(failure.exception.code, 3)
            self.assertIn('Rosetta', str(failure.exception))

    def test_shared_registration_requires_all_repositories_scope(self):
        selection = self.shared_selection()
        with patch.object(cli, 'load_config', return_value=selection), patch.object(cli, 'manual_register_runner') as manual, patch.object(cli, 'operation_lock'):
            with self.assertRaises(cli.Failure) as failure:
                cli.main(['--repo', 'other/two', 'register'])
            self.assertEqual(failure.exception.code, 2)
            manual.assert_not_called()
            self.assertEqual(cli.main(['--repo', 'other/two', 'register', '--manual-token', '--all-repos']), 0)
        manual.assert_called_once()

    def test_stopped_resume_never_starts_vm(self):
        events = []
        with patch.object(cli, 'vm_state', side_effect=['Stopped', 'Running']), patch.object(cli, 'lima', side_effect=lambda *a: events.append('start') or ''), patch.object(cli, 'group_contract', side_effect=cli.Failure('mismatch', 3)), patch.object(cli, 'guest') as guest:
            with self.assertRaises(cli.Failure):
                cli.maintain(cli.Selection(self.config, (self.config,)), 'resume', cli.deadline(1))
            guest.assert_not_called()
        self.assertEqual(events, [])

    def test_restart_preserves_pause(self):
        commands = []
        with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'package_gate', return_value=False), patch.object(cli, 'idle', return_value=True), patch.object(cli, 'lima', side_effect=lambda config, args, until: commands.append(args) or ''), patch.object(cli, 'guest') as guest:
            cli.maintain(cli.Selection(self.config, (self.config,)), 'restart', cli.deadline(1))
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
                with patch.object(cli, 'vm_state', return_value='Running'), patch.object(cli, 'group_contract'), patch.object(cli, 'package_gate', return_value=False), patch.object(cli, 'lima') as lima:
                    with self.assertRaises(cli.Failure):
                        cli.maintain(cli.Selection(config, (config,)), 'restart', cli.deadline(1))
                    lima.assert_not_called()
                path.unlink()

    def test_alias_service_is_readonly(self):
        with patch.object(cli, 'guest') as guest, self.assertRaises(cli.Failure):
            cli.verify_contract(dict(self.config, unit='alias.service'), cli.deadline(1))
        guest.assert_not_called()

    def test_service_contract_requires_exact_resolved_file(self):
        observed = dict(LoadState='loaded', FragmentPath='/etc/systemd/user/ci-vm-runner.service',
                        DropInPaths='', NeedDaemonReload='no', Transient='no',
                        UnitHash=hashlib.sha256((ROOT / 'config/ci-vm-runner.service').read_bytes()).hexdigest(),
                        UnitOwner='0:644', ActualUID='1001', MarkerOwner='0:755')
        for changes in ({}, {'FragmentPath': '/home/ci/ci-vm-runner.service'},
                        {'FragmentPath': '/etc/xdg/systemd/user/ci-vm-runner.service'},
                        {'UnitHash': '0' * 64}, {'UnitOwner': '1001:644'},
                        {'DropInPaths': '/home/ci/override.conf'}, {'NeedDaemonReload': 'yes'}):
            output = ''.join(f'{key}={value}\n' for key, value in dict(observed, **changes).items())
            with self.subTest(changes=changes), patch.object(cli, 'guest', return_value=output):
                if changes:
                    with self.assertRaises(cli.Failure):
                        cli.verify_contract(self.config, cli.deadline(1))
                else:
                    cli.verify_contract(self.config, cli.deadline(1))

    def test_stopped_pause_refuses(self):
        with patch.object(cli, 'vm_state', return_value='Stopped'), patch.object(cli, 'guest') as guest:
            with self.assertRaises(cli.Failure):
                cli.maintain(cli.Selection(self.config, (self.config,)), 'pause', cli.deadline(1))
            guest.assert_not_called()


if __name__ == '__main__':
    unittest.main()
