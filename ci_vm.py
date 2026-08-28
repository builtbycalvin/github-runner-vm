#!/usr/bin/env python3
"""Manage one Lima runner without signalling running jobs."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

VERSION = 1
OWNER = 'github-runner-vm installation v1\n'
ROOT = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}\Z')
UNIT = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,150}\.service\Z')
DIAGNOSTICS = {
    'FAIL: CI user belongs to a privileged group',
    'FAIL: CI user has passwordless sudo',
    'FAIL: Docker is not rootless',
    'FAIL: shared filesystem mounted',
    'FAIL: AppArmor is disabled or unavailable',
    'FAIL: rootful Docker socket exists',
    'FAIL: CI UID differs',
    'FAIL: required firewall table is unavailable',
}


class Failure(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def deadline(seconds):
    return time.monotonic() + seconds


def run(argv, until, *, env=None, input=None):
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise Failure('Command deadline expired. No guest process was signalled.', 4)
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=env, start_new_session=True)
    except OSError as error:
        raise Failure(f'Cannot run {argv[0]}: {error}') from error
    try:
        output, error = process.communicate(input, timeout=remaining)
    except subprocess.TimeoutExpired:
        cleanup_warning = ''
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            cleanup_warning = ' Host process-group cleanup was denied; child processes may still be running.'
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        raise Failure('Host command timed out. Guest state is unconfirmed; no guest stop or kill was requested.' + cleanup_warning, 4)
    if process.returncode:
        diagnostics = [line for line in output.splitlines() if line in DIAGNOSTICS]
        details = ' '.join(diagnostics) or 'No recognized diagnostic. Inspect locally with doctor or logs; do not publish raw logs.'
        raise Failure(f'{Path(argv[0]).name} failed ({process.returncode}). {details}')
    return output


def validate_config(config):
    required = {'version', 'vm', 'lima_home', 'guest_user', 'guest_uid', 'unit'}
    if not isinstance(config, dict) or not required <= config.keys() or config.keys() - required - {'repo', 'runner_id'}:
        raise Failure('Invalid configuration fields.', 2)
    if type(config['version']) is not int or config['version'] != VERSION:
        raise Failure('Unsupported configuration version.', 2)
    for field in ('vm', 'guest_user'):
        if not isinstance(config[field], str) or not IDENTIFIER.fullmatch(config[field]):
            raise Failure(f'Invalid {field}.', 2)
    if not isinstance(config['unit'], str) or not UNIT.fullmatch(config['unit']):
        raise Failure('Invalid service unit.', 2)
    if type(config['guest_uid']) is not int or not 1 <= config['guest_uid'] <= 2147483647:
        raise Failure('Invalid guest UID.', 2)
    if not isinstance(config['lima_home'], str) or not Path(config['lima_home']).is_absolute():
        raise Failure('lima_home must be absolute.', 2)
    if ('repo' in config) != ('runner_id' in config):
        raise Failure('--repo and --runner-id must be supplied together.', 2)
    if 'repo' in config:
        if not isinstance(config['repo'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config['repo']):
            raise Failure('Invalid repository.', 2)
        if type(config['runner_id']) is not int or config['runner_id'] <= 0:
            raise Failure('Invalid runner ID.', 2)
    return config


def paths():
    home = Path.home()
    return home, home / '.local/share/github-runner-vm', home / '.config/github-runner-vm', home / '.local/bin/ci-vm'


def safe_path(path, home):
    if home.is_symlink() or not home.is_dir() or home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o022:
        raise Failure('HOME must be a user-owned directory without a symlink or group/world write permissions.', 2)
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise Failure('Install paths must remain inside the current home.', 2)
    current = home
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise Failure(f'Refusing symlink at {current}.', 2)
        if current.exists() and (current.stat().st_uid != os.getuid() or current.stat().st_mode & 0o022):
            raise Failure(f'Refusing an unowned or group/world writable path: {current}.', 2)


def owned_directory(path, mode):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.ci-vm-', dir=path.parent))
    try:
        (temporary / '.github-runner-vm-owner').write_text(OWNER)
        temporary.chmod(mode)
        os.rename(temporary, path)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def atomic_write(path, data, mode):
    descriptor, name = tempfile.mkstemp(prefix='.ci-vm-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def strict_json(text):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique_object)


def load_config():
    home, _, directory, _ = paths()
    path = directory / 'config.json'
    safe_path(path, home)
    try:
        if path.stat().st_mode & 0o077:
            raise Failure('Configuration must have mode 0600.', 2)
        return validate_config(strict_json(path.read_text()))
    except (OSError, ValueError) as error:
        raise Failure(f'Cannot read configuration. Run install.sh first. {error}', 2) from error


def lima(config, args, until, input=None):
    env = dict(os.environ, LIMA_HOME=config['lima_home'])
    return run(['limactl', *args], until, env=env, input=input)


def vm_state(config, until):
    output = lima(config, ['list', '--json'], until)
    try:
        stripped = output.strip()
        entries = strict_json(stripped) if stripped.startswith('[') else [strict_json(line) for line in output.splitlines() if line.strip()]
        if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get('name'), str) for item in entries):
            raise ValueError('Unexpected list format')
        matches = [item for item in entries if item['name'] == config['vm']]
        if not matches:
            return 'Absent'
        if len(matches) != 1 or matches[0].get('status') not in {'Running', 'Stopped', 'Broken', 'Uninitialized', 'Starting', 'Stopping'}:
            raise ValueError('Unknown VM state')
        return matches[0]['status']
    except (ValueError, TypeError) as error:
        raise Failure('Cannot establish VM state from Lima JSON.', 3) from error


def guest(config, script, until, *args):
    return lima(config, ['shell', config['vm'], '--', 'bash', '-s', '--',
                         config['guest_user'], str(config['guest_uid']), config['unit'], *args], until, input=script)


USER_ENV = '''set -euo pipefail
user=$1
uid=$2
unit=$3
ctl() { sudo -n runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user "$@"; }
docker_ci() { sudo -n runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" DOCKER_HOST="unix:///run/user/$uid/docker.sock" docker "$@"; }
'''


def fields(output):
    parsed = {}
    for line in output.splitlines():
        key, separator, value = line.partition('=')
        if not separator or key in parsed:
            raise Failure('Unknown or duplicate guest output.', 3)
        parsed[key] = value
    return parsed


def verify_contract(config, until):
    if (config['guest_user'], config['guest_uid'], config['unit']) != ('ci', 1001, 'ci-vm-runner.service'):
        raise Failure('Maintenance requires the supplied ci UID 1001 service contract. Adoption remains read-only.', 3)
    script = USER_ENV + '''ctl show "$unit" --property=LoadState,FragmentPath,DropInPaths,NeedDaemonReload,Transient
fragment=$(ctl show "$unit" --property=FragmentPath --value)
test -n "$fragment"
printf 'UnitHash='
sudo -n sha256sum "$fragment" | cut -d ' ' -f 1
printf 'UnitOwner='
sudo -n stat -c '%u:%a' "$fragment"
printf 'ActualUID=%s\\n' "$(id -u "$user")"
printf 'MarkerOwner='
sudo -n stat -c '%u:%a' /var/lib/ci-vm
'''
    observed = fields(guest(config, script, until))
    expected_hash = hashlib.sha256((ROOT / 'config/ci-vm-runner.service').read_bytes()).hexdigest()
    expected = {'LoadState': 'loaded', 'FragmentPath': '/etc/systemd/user/' + config['unit'],
                'DropInPaths': '', 'NeedDaemonReload': 'no', 'Transient': 'no', 'UnitHash': expected_hash,
                'UnitOwner': '0:644', 'ActualUID': '1001', 'MarkerOwner': '0:755'}
    if observed != expected:
        raise Failure('Service contract differs or is inconclusive. Maintenance refused; no service migration is automatic.', 3)


def idle(config, until):
    script = USER_ENV + '''ctl show "$unit" --property=ActiveState,SubState,MainPID,ControlPID,ControlGroup,Job
printf 'Paused='
if sudo -n test -f /var/lib/ci-vm/paused; then echo yes; else echo no; fi
printf 'Runners='
ps -eo comm= | awk '$1 == "Runner.Listener" || $1 == "Runner.Worker" {n++} END {print n+0}'
printf 'Containers='
docker_ci ps -q | awk 'END {print NR+0}'
printf 'Jobs='
ctl list-jobs --no-legend --no-pager | awk 'END {print NR+0}'
printf 'CgroupEmpty='
cg=$(ctl show "$unit" --property=ControlGroup --value)
if [ -z "$cg" ]; then echo yes; else
  case "$cg" in /user.slice/*) ;; *) exit 9;; esac
  sudo -n bash -s -- "/sys/fs/cgroup$cg" <<'CGROUP'
set -euo pipefail
if [ ! -d "$1" ]; then echo yes; exit; fi
files=$(find "$1" -name cgroup.procs -type f)
test -n "$files"
while IFS= read -r file; do if [ -n "$(cat "$file")" ]; then echo no; exit; fi; done <<< "$files"
echo yes
CGROUP
fi
'''
    data = fields(guest(config, script, until))
    required = {'ActiveState', 'SubState', 'MainPID', 'ControlPID', 'ControlGroup', 'Job', 'Paused', 'Runners', 'Containers', 'Jobs', 'CgroupEmpty'}
    if data.keys() != required or data['Paused'] not in {'yes', 'no'} or data['CgroupEmpty'] not in {'yes', 'no'}:
        raise Failure('Idle probe is inconclusive.', 3)
    if any(not data[key].isdigit() for key in ('MainPID', 'ControlPID', 'Runners', 'Containers', 'Jobs')):
        raise Failure('Idle probe returned unknown counts.', 3)
    return data['ActiveState'] == 'inactive' and data['SubState'] == 'dead' and data['MainPID'] == data['ControlPID'] == data['Runners'] == data['Containers'] == data['Jobs'] == '0' and data['Job'] in {'', '0'} and data['Paused'] == data['CgroupEmpty'] == 'yes'


def maintain(config, command, until):
    state = vm_state(config, until)
    if state == 'Stopped' and command == 'resume':
        raise Failure('Resume does not boot an unverified VM. Explicitly start your known existing VM after reviewing its configuration, then run doctor and resume.', 3)
    if state != 'Running':
        raise Failure(f'Maintenance requires a running existing VM; found {state}.', 3)
    verify_contract(config, until)
    if command == 'resume':
        guest(config, USER_ENV + 'sudo -n rm -f -- /var/lib/ci-vm/paused\nctl start --no-block "$unit"\n', until)
        print('Resume requested. Run status and verify the exact runner in GitHub.')
        return
    if command == 'restart':
        reject_inherited_config(config)
        if not idle(config, until):
            raise Failure('Restart requires an already paused, inactive runner, no containers, no runner processes, and no systemd jobs.', 3)
        verify_contract(config, until)
        if not idle(config, until):
            raise Failure('Runner state changed. Restart refused.', 3)
        lima(config, ['stop', config['vm']], until)
        reject_inherited_config(config)
        lima(config, ['start', '--tty=false', config['vm']], until)
        verify_contract(config, until)
        if not idle(config, until):
            raise Failure('VM restarted but paused idle state could not be verified.', 3)
        print('VM restarted. Runner remains paused; resume explicitly when ready.')
        return
    guest(config, USER_ENV + 'sudo -n touch -- /var/lib/ci-vm/paused\n', until)
    while time.monotonic() < until:
        if idle(config, until):
            print('Paused. VM remains running; the runner service is inactive.')
            return
        time.sleep(min(0.2, max(0, until - time.monotonic())))
    raise Failure('Pause pending. A listener may accept one final job. Marker remains set; no process was stopped.', 4)


def reject_inherited_config(config):
    for name in ('default.yaml', 'override.yaml', 'base.yaml'):
        path = Path(config['lima_home']) / '_config' / name
        if path.exists() or path.is_symlink():
            raise Failure('VM creation or restart refuses inherited Lima configuration, including dangling symlinks.', 3)


@contextmanager
def operation_lock():
    home = Path.home()
    safe_path(home, home)
    descriptor = os.open(home, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('Another installation or maintenance command is running.', 3)
        yield
    finally:
        os.close(descriptor)


def report(config, command, until, lines=100):
    state = vm_state(config, until)
    print(f'VM {config["vm"]}: {state}')
    if state != 'Running':
        if command != 'status':
            raise Failure('Read-only checks never start a stopped or absent VM.')
        return
    if command == 'logs':
        print(guest(config, USER_ENV + 'sudo -n runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" journalctl --user -u "$unit" --no-pager -n "$4"\n', until, str(lines)), end='')
        return
    print(guest(config, USER_ENV + 'ctl show "$unit" --property=LoadState,ActiveState,SubState,MainPID\n', until), end='')
    if 'repo' in config:
        output = run(['gh', 'api', '--hostname', 'github.com', f'repos/{config["repo"]}/actions/runners/{config["runner_id"]}'], until)
        try:
            runner = strict_json(output)
            if type(runner.get('id')) is not int or runner.get('id') != config['runner_id'] or type(runner.get('busy')) is not bool or runner.get('status') not in {'online', 'offline'}:
                raise ValueError('Identity or state mismatch')
        except (ValueError, AttributeError) as error:
            raise Failure('GitHub runner readback is inconclusive.', 3) from error
        print(f'GitHub runner {runner["id"]}: {runner["status"]}, busy={runner["busy"]}')
    if command != 'doctor':
        return
    failures = []
    print(f'Host: {platform.system()} {platform.machine()}')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        failures.append('Host is not macOS Apple Silicon.')
    print(lima(config, ['--version'], until).strip())
    try:
        verify_contract(config, until)
        print('Service contract: verified')
    except Failure as error:
        failures.append(str(error))
    script = USER_ENV + '''uname -sm
id "$user"
case " $(id -Gn "$user") " in *" sudo "*|*" wheel "*|*" admin "*|*" docker "*|*" lxd "*|*" disk "*|*" root "*) echo 'FAIL: CI user belongs to a privileged group'; exit 1;; esac
if sudo -n runuser -u "$user" -- sudo -n true; then echo 'FAIL: CI user has passwordless sudo'; exit 1; fi
test "$(id -u "$user")" = "$uid" || { echo 'FAIL: CI UID differs'; exit 1; }
security=$(docker_ci info --format '{{json .SecurityOptions}}')
printf '%s\\n' "$security"
case "$security" in *rootless*) ;; *) echo 'FAIL: Docker is not rootless'; exit 1;; esac
printf 'AppArmor: '
test "$(cat /sys/module/apparmor/parameters/enabled)" = Y || { echo 'FAIL: AppArmor is disabled or unavailable'; exit 1; }
echo enabled
test ! -S /var/run/docker.sock || { echo 'FAIL: rootful Docker socket exists'; exit 1; }
mounts=$(findmnt -rn -o FSTYPE,TARGET)
printf '%s\\n' "$mounts"
if printf '%s\\n' "$mounts" | grep -Eq '^(virtiofs|9p|fuse.sshfs) '; then echo 'FAIL: shared filesystem mounted'; exit 1; fi
sudo -n nft list table inet ci_vm || { echo 'FAIL: required firewall table is unavailable'; exit 1; }
'''
    try:
        print(guest(config, script, until), end='')
    except Failure as error:
        failures.append(str(error))
    config_path = Path(config['lima_home']) / config['vm'] / 'lima.yaml'
    try:
        contents = config_path.read_text()
        print('Instance Lima configuration available. Manual review required for mounts, forwarding, inherited configuration, proxy and credentials.')
        if re.search(r'forwardAgent:\s*true|forwardX11:\s*true|loadDotSSHPubKeys:\s*true', contents):
            failures.append('Instance configuration enables forwarding or host SSH public key loading.')
    except OSError:
        failures.append('Effective Lima configuration unavailable for manual review.')
    print('INCONCLUSIVE: network isolation and inherited Lima configuration require the documented manual checks. Health checks are not proof against compromise.')
    if failures:
        raise Failure('\n'.join(failures))


def launcher_text(interpreter, module):
    return '#!/bin/sh\n# github-runner-vm installation v1\nexec ' + shlex.quote(interpreter) + ' ' + shlex.quote(str(module)) + ' "$@"\n'


def recognized_launcher(contents, module):
    lines = contents.splitlines()
    if len(lines) != 3:
        return False
    try:
        words = shlex.split(lines[2])
    except ValueError:
        return False
    return len(words) == 4 and words[0] == 'exec' and Path(words[1]).is_absolute() and words[2] == str(module) and words[3] == '$@' and contents == launcher_text(words[1], module)


PATH_BLOCK = b'''# >>> github-runner-vm PATH >>>
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
# <<< github-runner-vm PATH <<<
'''


def shell_updates(home):
    login = next((home / name for name in ('.bash_profile', '.bash_login', '.profile')
                  if (home / name).exists() or (home / name).is_symlink()), home / '.bash_profile')
    zsh_directory = Path(os.environ.get('ZDOTDIR') or str(home))
    if not zsh_directory.is_absolute() or '..' in zsh_directory.parts:
        raise Failure('ZDOTDIR must be an absolute path inside HOME without parent traversal.', 2)
    updates = []
    for path in (home / '.bashrc', login, zsh_directory / '.zshrc'):
        safe_path(path, home)
        if path.exists() and not path.is_file():
            raise Failure(f'Shell startup path is not a regular file: {path}', 2)
        data = path.read_bytes() if path.exists() else b''
        start, end = PATH_BLOCK.splitlines()[0], PATH_BLOCK.splitlines()[-1]
        if start in data or end in data:
            if data.count(start) != 1 or data.count(end) != 1 or PATH_BLOCK not in data:
                raise Failure(f'Edited PATH block at {path}. Preserve it and resolve manually.', 2)
            continue
        separator = b'\n' if data and not data.endswith(b'\n') else b''
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        updates.append((path, data if path.exists() else None, data + separator + PATH_BLOCK, mode))
    return updates


def install(args):
    if os.geteuid() == 0:
        raise Failure('Install as your normal macOS user, not root.', 2)
    home, share, directory, launcher = paths()
    for path in (share, directory, launcher):
        safe_path(path, home)
    owner_file = share / '.github-runner-vm-owner'
    config_owner = directory / '.github-runner-vm-owner'
    for path in (owner_file, config_owner):
        safe_path(path, home)
    if share.exists() and (not owner_file.is_file() or owner_file.read_text() != OWNER):
        raise Failure('Existing share directory is not owned by this installer.', 2)
    if directory.exists() and (not config_owner.is_file() or config_owner.read_text() != OWNER):
        raise Failure('Existing configuration directory is not owned by this installer.', 2)
    launch_content = launcher_text(str(Path(sys.executable).resolve()), share / 'ci_vm.py')
    if launcher.exists() and (not owner_file.is_file() or not recognized_launcher(launcher.read_text(), share / 'ci_vm.py')):
        raise Failure('Existing ci-vm launcher differs. Preserve it and resolve manually.', 2)
    vm = args.adopt or args.provision
    lima_home = str(Path(args.lima_home).expanduser().absolute()) if args.lima_home else str(home / ('.lima' if args.adopt else '.local/share/github-runner-vm/lima'))
    config = dict(version=VERSION, vm=vm, lima_home=lima_home, guest_user=args.guest_user, guest_uid=args.guest_uid, unit=args.unit)
    if args.repo is not None:
        config['repo'] = args.repo
    if args.runner_id is not None:
        config['runner_id'] = args.runner_id
    validate_config(config)
    config_path = directory / 'config.json'
    safe_path(config_path, home)
    if config_path.exists() and load_config() != config:
        raise Failure('Existing configuration differs. No adoption or overwrite performed.', 2)
    startup_updates = shell_updates(home) if args.configure_shell else []
    until = deadline(args.timeout)
    state = vm_state(config, until)
    if args.adopt and state == 'Absent':
        raise Failure('The named VM does not exist. Adoption never provisions.', 2)
    if args.provision:
        if (args.guest_user, args.guest_uid, args.unit) != ('ci', 1001, 'ci-vm-runner.service'):
            raise Failure('Provisioning uses the supplied ci UID 1001 service contract. Custom identities are adoption-only.', 2)
        if not args.yes_create_vm:
            raise Failure('Provisioning requires --yes-create-vm.', 2)
        if platform.system() != 'Darwin' or platform.machine() != 'arm64':
            raise Failure('Provisioning requires an Apple Silicon Mac.', 2)
        if state != 'Absent' or (Path(lima_home) / vm).exists() or (Path(lima_home) / vm).is_symlink():
            raise Failure('VM already exists. Provisioning never replaces or recreates it.', 2)
        version = lima(config, ['--version'], until)
        match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (2, 2, 0):
            raise Failure('Provisioning requires Lima 2.2.0 or newer.', 2)
        reject_inherited_config(config)
    sources = {name: (ROOT / 'config' / name).read_bytes() for name in ('lima.yaml', 'provision.sh', 'ci-vm-runner.service')}
    module_bytes = Path(__file__).read_bytes()
    for path in (share / 'ci_vm.py', share / 'config', *(share / 'config' / name for name in sources)):
        safe_path(path, home)
    owned_directory(share, 0o755)
    owned_directory(directory, 0o700)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    for path in (share / 'ci_vm.py', owner_file, share / 'config'):
        safe_path(path, home)
    (share / 'config').mkdir(exist_ok=True)
    for name, contents in sources.items():
        target = share / 'config' / name
        safe_path(target, home)
        atomic_write(target, contents, 0o644)
    atomic_write(share / 'ci_vm.py', module_bytes, 0o644)
    atomic_write(owner_file, OWNER.encode(), 0o600)
    atomic_write(config_path, (json.dumps(config, indent=2) + '\n').encode(), 0o600)
    atomic_write(launcher, launch_content.encode(), 0o755)
    for path, original, data, mode in startup_updates:
        safe_path(path, home)
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_path(path, home)
        if (path.read_bytes() if path.exists() else None) != original:
            raise Failure(f'Shell startup file changed during install: {path}. Rerun after reviewing it.', 2)
        atomic_write(path, data, mode)
        print(f'Added PATH block to {path}', flush=True)
    if args.provision:
        print('Creating the explicitly requested VM. A timeout leaves its state intact for inspection.', flush=True)
        reject_inherited_config(config)
        lima(config, ['start', '--tty=false', '--name', vm, str(share / 'config/lima.yaml')], until)
    print('Installed ci-vm. Run now: "$HOME/.local/bin/ci-vm" status')
    if args.configure_shell:
        print('PATH configured for future Bash and Zsh sessions. Open a new terminal, then run ci-vm status.')
    else:
        print('No shell startup files were changed. To use ci-vm here: export PATH="$HOME/.local/bin:$PATH"')
    print('Adoption does not change the VM or its runner registration.' if args.adopt else 'VM provision request completed. Register the runner with a short-lived token in the guest.')


def timeout_value(value):
    try:
        number = float(value)
        if not 0 < number <= 3600:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError('timeout must be greater than zero and at most 3600 seconds')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    if argv and argv[0] == '--install':
        parser.add_argument('--install', action='store_true', help=argparse.SUPPRESS)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--adopt', metavar='NAME')
        mode.add_argument('--provision', metavar='NAME')
        parser.add_argument('--yes-create-vm', action='store_true')
        parser.add_argument('--configure-shell', action='store_true', help='append a managed PATH block to Bash and Zsh startup files')
        parser.add_argument('--lima-home')
        parser.add_argument('--guest-user', default='ci')
        parser.add_argument('--guest-uid', type=int, default=1001)
        parser.add_argument('--unit', default='ci-vm-runner.service')
        parser.add_argument('--repo')
        parser.add_argument('--runner-id', type=int)
        parser.add_argument('--timeout', type=timeout_value, default=600)
        args = parser.parse_args(argv)
        with operation_lock():
            install(args)
        return 0
    parser.add_argument('--timeout', type=timeout_value, default=30)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'doctor', 'logs', 'pause', 'resume', 'restart'):
        sub = commands.add_parser(name)
        sub.add_argument('--timeout', type=timeout_value, default=argparse.SUPPRESS)
        if name == 'logs':
            sub.add_argument('--lines', type=int, choices=range(1, 1001), default=100, metavar='1..1000')
    args = parser.parse_args(argv)
    until = deadline(args.timeout)
    if args.command in {'pause', 'resume', 'restart'}:
        with operation_lock():
            maintain(load_config(), args.command, until)
    else:
        report(load_config(), args.command, until, getattr(args, 'lines', 100))
    return 0


if __name__ == '__main__':
    try:
        if sys.version_info < (3, 10):
            raise Failure('Python 3.10 or newer is required.', 2)
        sys.exit(main())
    except Failure as error:
        print(str(error), file=sys.stderr)
        sys.exit(error.code)
    except (OSError, ValueError) as error:
        print(f'Cannot complete operation: {error}', file=sys.stderr)
        sys.exit(1)
