import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AccountProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ci-vm-account-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'accounts.json'
        self.state.write_text('{}')
        program = self.root / 'accounts'
        program.write_text('#!' + sys.executable + '''
import json, os, pathlib, sys
path = pathlib.Path(os.environ['ACCOUNT_STATE'])
state = json.loads(path.read_text())
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if command == 'id':
    sys.exit(0 if state.get('user') == 'ci' else 1)
elif command == 'getent':
    key = 'user' if args[0] == 'passwd' else 'group'
    name = state.get(key)
    if not name or args[1] not in (name, '1001'):
        sys.exit(2)
    print(name + ':x:1001:')
elif command == 'groupadd':
    state['group'] = 'ci'
elif command == 'useradd':
    if os.environ.get('FAIL_USERADD') == '1':
        sys.exit(1)
    state['user'] = 'ci'
path.write_text(json.dumps(state))
''')
        program.chmod(0o755)
        for command in ('id', 'getent', 'groupadd', 'useradd'):
            (self.root / command).symlink_to(program)
        source = (ROOT / 'config/provision.sh').read_text()
        self.script = 'set -euo pipefail\n' + source.split('if ! id ci', 1)[1].split('test "$(id -u ci)', 1)[0]
        self.script = self.script.replace('set -euo pipefail\n', 'set -euo pipefail\nif ! id ci', 1)
        self.env = dict(os.environ, ACCOUNT_STATE=str(self.state), PATH=str(self.root) + os.pathsep + os.environ['PATH'])

    def provision(self, fail=False):
        return subprocess.run(['bash', '-c', self.script], env=dict(self.env, FAIL_USERADD='1' if fail else '0'),
                              capture_output=True, text=True, timeout=5)

    def test_rerun_after_group_created(self):
        self.assertNotEqual(self.provision(fail=True).returncode, 0)
        self.assertEqual(json.loads(self.state.read_text()), {'group': 'ci'})
        result = self.provision()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.state.read_text()), {'group': 'ci', 'user': 'ci'})
        self.assertEqual(self.provision().returncode, 0)

    def test_foreign_user_and_group_are_preserved(self):
        for state in ({'user': 'other'}, {'group': 'other'}):
            with self.subTest(state=state):
                self.state.write_text(json.dumps(state))
                self.assertNotEqual(self.provision().returncode, 0)
                self.assertEqual(json.loads(self.state.read_text()), state)

    def test_baseline_installs_lttng_runtime_directly(self):
        source = (ROOT / 'config/provision.sh').read_text()
        baseline = source.split('apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y', 1)[1]
        baseline = baseline.split('\n\nif ! id ci', 1)[0]
        self.assertIn('liblttng-ust1t64', baseline.split())


class SharedPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ci-vm-shared-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.units = self.root / 'units'
        self.tools = self.root / 'tools'
        self.staging = self.root / 'staging'
        for path in (self.state, self.units, self.tools, self.staging):
            path.mkdir()
        self.runtime = self.root / 'run/user/1001'
        self.runtime.mkdir(parents=True)
        (self.runtime / 'docker.sock').touch()
        self.docker_fragment = self.root / 'ci/.config/systemd/user/docker.service'
        self.docker_fragment.parent.mkdir(parents=True)
        self.docker_fragment.write_text('[Service]\nExecStart=dockerd-rootless.sh\n')
        self.docker_cgroup = '/user.slice/user-1001.slice/user@1001.service/app.slice/docker.service'
        for pid in ('10', '11'):
            process = self.root / 'proc' / pid
            process.mkdir(parents=True)
            (process / 'cgroup').write_text('0::' + self.docker_cgroup + '\n')
        (self.state / 'paused').touch()
        self.key = 'other-repo-123456abcdef'
        self.unit = 'ci-vm-runner@' + self.key + '.service'
        self.control = self.root / 'control.json'
        self.control.write_text('{}')
        replacements = {'/var/lib/ci-vm': str(self.state), '/etc/systemd/user': str(self.units),
                        '/home/ci': str(self.root / 'ci'), '/home/$user': str(self.root / 'ci'), '/sys/fs/cgroup': str(self.root / 'cgroups'),
                        '/run/user': str(self.root / 'run/user'), '/proc/': str(self.root / 'proc') + '/',
                        'runtime_base=/run': 'runtime_base=' + str(self.root / 'run')}
        for name in ('prepare-shared-runner.sh', 'container-runtime-state.sh', 'ci-vm-runner@.service', 'ci-vm-runner.service'):
            text = (ROOT / 'config' / name).read_text()
            for original, replacement in replacements.items():
                text = text.replace(original, replacement)
            (self.staging / name).write_text(text)
        (self.units / 'ci-vm-runner.service').write_bytes((self.staging / 'ci-vm-runner.service').read_bytes())
        stub = self.tools / 'stub'
        stub.write_text('#!' + sys.executable + '\n' + r'''import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['SHARED_TEST_ROOT'])
state = json.loads((root / 'control.json').read_text())
with (root / 'calls.jsonl').open('a') as out:
    out.write(json.dumps([name, args]) + '\n')
if name == state.get('fail_tool'):
    sys.exit(9)
if name == 'id':
    print('ci' if '-Gn' in args else '1001' if 'ci' in args else '0')
elif name == 'stat':
    path = pathlib.Path(args[-1])
    if not path.exists(): sys.exit(1)
    fmt = args[1]
    if str(path) == state.get('bad_owner'): print('1001:777')
    elif fmt == '%u': print('0')
    elif fmt == '%u:%g': print(state.get('socket_owner', '1001:1001'))
    elif path.name == 'docker.service' and fmt == '%u:%g:%a': print('1001:1001:644')
    elif fmt == '%u:%g:%a': print('1001:1001:700')
    else: print('0:' + oct(path.stat().st_mode & 0o777)[2:])
elif name == 'flock':
    pass
elif name == 'readlink':
    print(pathlib.Path(args[-1]).resolve())
elif name == 'ps':
    print(state.get('runtime_processes', '10 1001 dockerd\n11 1001 containerd') if any('uid=' in arg for arg in args) else state.get('processes', ''))
elif name == 'find' and str(root / 'run') in args:
    print(state.get('runtime_sockets', str(root / 'run/user/1001/docker.sock')))
elif name == 'find':
    os.execv('/usr/bin/find', ['find', *args])
elif name == 'cat':
    path = pathlib.Path(args[-1])
    if path.name == 'cgroup.procs' and state.get('fail_cgroup_read'): sys.exit(8)
    sys.stdout.write(path.read_text())
elif name == 'install':
    path = pathlib.Path(args[-1])
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    if state.get('fail_after_directory') == path.name: sys.exit(8)
elif name == 'runuser':
    if 'docker' in args:
        if state.get('fail_docker') or state.get('fail_docker_info') and 'info' in args or state.get('fail_docker_ps') and 'ps' in args: sys.exit(8)
        sys.stdout.write('["name=rootless"]\n' if 'info' in args else state.get('containers', ''))
        sys.exit(0)
    args = args[args.index('systemctl') + 2:]
    action = args[0]
    if action == state.get('fail_ctl'): sys.exit(8)
    if action == 'daemon-reload':
        state['reload'] = 'no'
        for values in state.get('properties', {}).values(): values['NeedDaemonReload'] = 'no'
        (root / 'control.json').write_text(json.dumps(state))
    elif action == 'is-enabled':
        print('enabled' if args[1] in state.get('enabled', []) else 'disabled')
        sys.exit(0 if args[1] in state.get('enabled', []) else 1)
    elif action == 'list-jobs': print(state.get('jobs', ''))
    elif action in ('list-units', 'list-unit-files'):
        for path in (root / 'units').glob('ci-vm-runner*.service'): print(path.name + ' loaded')
        if state.get('extra_unit'): print(state['extra_unit'] + ' loaded')
    elif action == 'show':
        unit = args[1]
        prop = next(a.split('=', 1)[1] for a in args if a.startswith('--property='))
        if prop == state.get('fail_property'): sys.exit(8)
        values = {'FragmentPath': str(root / 'units' / unit), 'LoadState': 'loaded', 'DropInPaths': '',
                  'NeedDaemonReload': state.get('reload', 'no'), 'Transient': 'no', 'ActiveState': 'inactive',
                  'SubState': 'dead', 'MainPID': '0', 'ControlPID': '0', 'Job': '', 'ControlGroup': ''}
        if unit == 'docker.service':
            values.update({'FragmentPath': str(root / 'ci/.config/systemd/user/docker.service'), 'ActiveState': 'active',
                           'SubState': 'running', 'ControlGroup': state.get('docker_cgroup', '/user.slice/user-1001.slice/user@1001.service/app.slice/docker.service')})
        values.update(state.get('properties', {}).get(unit, {}))
        print(values[prop])
    else: sys.exit(7)
elif name == 'systemctl':
    unit = args[1]
    prop = next(a.split('=', 1)[1] for a in args if a.startswith('--property='))
    if state.get('fail_system_unit') == unit: sys.exit(8)
    values = {'LoadState': 'masked', 'UnitFileState': 'masked', 'ActiveState': 'inactive'}
    values.update(state.get('system_units', {}).get(unit, {}))
    print(values[prop])
elif name == 'sudo':
    os.execvp(args[1], args[1:])
else: sys.exit(6)
''')
        stub.chmod(0o755)
        for name in ('id', 'stat', 'flock', 'readlink', 'ps', 'find', 'cat', 'install', 'runuser', 'systemctl', 'sudo'):
            (self.tools / name).symlink_to(stub)
        self.env = dict(os.environ, SHARED_TEST_ROOT=str(self.root), PATH=str(self.tools) + os.pathsep + '/usr/bin:/bin')

    def invoke(self, action='prepare', ready=False):
        args = ['bash', str(self.staging / 'prepare-shared-runner.sh'), action, self.key]
        if ready:
            args.append('--registration-ready')
        return subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=15)

    def probe(self):
        command = 'source "$1"; ci_vm_container_runtime_state ci 1001'
        return subprocess.run(['bash', '-c', command, 'probe', str(self.staging / 'container-runtime-state.sh')],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def update(self, **values):
        state = json.loads(self.control.read_text())
        state.update(values)
        self.control.write_text(json.dumps(state))

    def ready(self):
        runner = self.root / 'ci/runners' / self.key / 'bin/Runner.Listener'
        runner.parent.mkdir()
        runner.touch(mode=0o700)
        self.update(enabled=[self.unit])

    def test_prepare_finish_and_exact_reruns_preserve_registration(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('run ci-vm register --all-repos for the selected repository profile', result.stdout)
        self.assertNotIn('enable the exact unit', result.stdout)
        self.assertNotIn('--registration-ready', result.stdout)
        gate = self.state / 'shared-setup'
        self.assertEqual(gate.read_text().strip(), self.key)
        credentials = self.root / 'ci/runners' / self.key / '.credentials'
        credentials.write_text('sentinel-never-read')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.invoke('finish').returncode, 0)
        self.assertNotEqual(self.invoke('finish', ready=True).returncode, 0)
        self.assertTrue(gate.exists())
        self.ready()
        result = self.invoke('finish', ready=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'Shared setup finished. VM remains paused.\n')
        self.assertFalse(gate.exists())
        self.assertEqual(self.invoke('finish', ready=True).returncode, 0)
        self.assertTrue((self.state / 'paused').exists())
        self.assertEqual(credentials.read_text(), 'sentinel-never-read')
        calls = (self.root / 'calls.jsonl').read_text()
        self.assertNotIn('.credentials', calls)
        self.assertNotIn('--now', calls)

    def test_failure_after_directory_and_unit_publication_keeps_recoverable_gate(self):
        self.update(fail_after_directory=self.key)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertTrue((self.state / 'shared-setup').exists())
        self.update(fail_after_directory=None, fail_ctl='daemon-reload')
        self.assertNotEqual(self.invoke().returncode, 0)
        unit = self.units / self.unit
        self.assertTrue(unit.is_file())
        self.assertEqual(unit.read_bytes(), (self.staging / 'ci-vm-runner@.service').read_bytes().replace(b'@KEY@', self.key.encode()))
        self.update(fail_ctl=None, properties={self.unit: {'NeedDaemonReload': 'yes'}})
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inventory_probe_failure_unknown_unit_and_unexplained_directory_refuse(self):
        for update in ({'fail_tool': 'ps'}, {'fail_ctl': 'list-units'}, {'fail_ctl': 'list-unit-files'}, {'fail_ctl': 'list-jobs'}, {'fail_docker': True}, {'fail_property': 'DropInPaths'}, {'fail_property': 'Job'}, {'extra_unit': 'ci-vm-runner@unknown.service'}, {'processes': 'Runner.Listener'}, {'containers': 'container1'}):
            with self.subTest(update=update):
                self.control.write_text(json.dumps(update))
                self.assertNotEqual(self.invoke().returncode, 0)
                self.assertFalse((self.state / 'shared-setup').exists())
        self.control.write_text('{}')
        target = self.root / 'ci/runners' / self.key
        target.mkdir(parents=True)
        (target / 'existing').write_text('preserve')
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertFalse((self.state / 'shared-setup').exists())
        self.assertEqual((target / 'existing').read_text(), 'preserve')

    def test_container_runtime_drift_refuses_shared_preparation(self):
        expected = str(self.root / 'run/user/1001/docker.sock')
        cases = (
            {'runtime_processes': '10 0 dockerd\n11 1001 containerd'},
            {'runtime_processes': '10 1001 dockerd\n11 1001 containerd\n12 1002 podman'},
            {'runtime_processes': '10 1001 dockerd\n11 1001 containerd\n12 0 systemd-nspawn'},
            {'runtime_processes': '10 1001 dockerd'},
            {'runtime_sockets': expected + '\n' + str(self.root / 'run/user/1002/docker.sock')},
            {'socket_owner': '0:0'},
            {'docker_cgroup': '/user.slice/user-1001.slice/user@1001.service/app.slice/other.service'},
            {'system_units': {'docker.service': {'LoadState': 'loaded'}}},
        )
        for update in cases:
            with self.subTest(update=update):
                self.control.write_text(json.dumps(update))
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Container runtime state', result.stderr)
                self.assertFalse((self.state / 'shared-setup').exists())

    def test_runtime_probe_uses_privileged_boundary_and_preserves_docker_failures(self):
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'Containers=0\nRuntimeDrift=no\n')
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls[0], ['sudo', ['-n', 'bash', '-s', '--', 'ci', '1001']])
        find_call = next(call for call in calls if call[0] == 'find')
        self.assertIn(str(self.runtime), find_call[1])
        self.control.write_text(json.dumps({'fail_docker_ps': True}))
        result = self.probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Containers=0', result.stdout)

    def test_different_gate_or_nonmatching_unit_is_never_overwritten(self):
        gate = self.state / 'shared-setup'
        gate.write_text('different-repo-000000000000\n')
        gate.chmod(0o600)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertIn('different-repo', gate.read_text())
        gate.write_text(self.key + '\n')
        (self.units / self.unit).write_text('unrelated unit\n')
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual((self.units / self.unit).read_text(), 'unrelated unit\n')

    def test_cgroup_read_failure_and_missing_finish_directories_refuse(self):
        cgroup = self.root / 'cgroups/user.slice/member'
        cgroup.mkdir(parents=True)
        (cgroup / 'cgroup.procs').touch()
        self.update(properties={'ci-vm-runner.service': {'ControlGroup': '/user.slice/member'}}, fail_cgroup_read=True)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / 'shared-setup').exists())
        self.update(fail_cgroup_read=False)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.ready()
        work = self.root / 'ci/work' / self.key
        work.rmdir()
        self.assertNotEqual(self.invoke('finish', ready=True).returncode, 0)
        self.assertFalse(work.exists())
        self.assertTrue((self.state / 'shared-setup').exists())


if __name__ == '__main__':
    unittest.main()
