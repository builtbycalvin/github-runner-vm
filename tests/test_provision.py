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
        self.root = Path(self.temp.name)
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


if __name__ == '__main__':
    unittest.main()
