#!/usr/bin/env python3
import argparse
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
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
from types import MappingProxyType
from ci_vm_checks import PackageRequest, package_request, package_observation, assess_run

VERSION = 1
OWNER = 'github-runner-vm installation v1\n'
ROOT = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}\Z')
UNIT = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,150}\.service\Z')
INSTALL_FILES = (
    'ci_vm_checks.py',
    'config/lima.yaml', 'config/provision.sh', 'config/ci-vm-runner.service',
    'config/ci-vm-runner@.service', 'config/prepare-shared-runner.sh', 'config/container-runtime-state.sh',
    'docs/setup.md', 'docs/maintenance.md', 'docs/security.md', 'docs/llm-setup.md', 'examples/smoke.yml',
)
RESOURCE_LIMITS = {'cpus': (1, 64), 'memory_gib': (1, 512), 'disk_gib': (8, 4096)}
RESOURCE_DEFAULTS = {'cpus': 2, 'memory_gib': 2, 'disk_gib': 20}
DARWIN_UNIX_PATH_BYTES = 103
LIMA_SOCKET_COMPONENT_RESERVE = 32
COMMANDS = (
    ('setup', 'Show the read-only guide for your GitHub repository'),
    ('status', 'Inspect the VM and runner'),
    ('doctor', 'Run read-only health checks'),
    ('logs', 'Read recent runner logs'),
    ('pause', 'Request a cooperative pause; one final job may run'),
    ('resume', 'Resume a verified runner in a running VM'),
    ('restart', 'Restart an already paused, idle VM'),
    ('profiles', 'List local repository profiles without querying VMs'),
    ('packages', 'Preview or apply an exact guest package request'),
    ('verify-run', 'Verify expected Actions jobs on an exact runner'),
    ('register', 'Register and verify this runner through authenticated gh'),
)
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
DEFAULT_REGISTRATION_LABELS = frozenset(('self-hosted', 'linux', 'arm64'))


class Failure(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Selection(Mapping):
    selected: Mapping
    members: tuple

    def __post_init__(self):
        object.__setattr__(self, 'selected', MappingProxyType(dict(self.selected)))
        object.__setattr__(self, 'members', tuple(MappingProxyType(dict(member)) for member in self.members))

    def __getitem__(self, key):
        return self.selected[key]

    def __iter__(self):
        return iter(self.selected)

    def __len__(self):
        return len(self.selected)


@dataclass(frozen=True)
class HostArchitecture:
    system: str
    process_machine: str
    hardware_machine: str
    translated: bool

    @property
    def supported(self):
        return self.system == 'Darwin' and self.hardware_machine == 'arm64' and (self.process_machine == 'arm64' or self.translated)

    def display(self):
        suffix = ' under Rosetta' if self.translated else ''
        return f'{self.system} {self.hardware_machine} (process {self.process_machine}{suffix})'


@dataclass(frozen=True)
class RegistrationTarget:
    repo: str
    url: str
    name: str
    labels: tuple[str, ...]
    runner_directory: str
    work_directory: str
    unit: str
    shared_key: str | None


@dataclass(frozen=True)
class LocalRegistration:
    runner_id: int
    name: str
    url: str
    work_directory: str


@dataclass(frozen=True)
class RemoteRegistration:
    runner_id: int
    name: str
    status: str
    busy: bool
    labels: tuple[str, ...]


def deadline(seconds):
    return time.monotonic() + seconds


def cleanup_process_group(process):
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
    return cleanup_warning


class ProcessSignalRelay:
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __init__(self):
        self.process = None
        self.received = []
        self.previous = {}

    def handle(self, signum, frame):
        self.received.append(signum)
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    def attach(self, process):
        self.process = process
        for signum in self.received:
            try:
                os.killpg(process.pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    def __enter__(self):
        try:
            for signum in self.signals:
                self.previous[signum] = signal.signal(signum, self.handle)
        except BaseException:
            for signum, handler in self.previous.items():
                signal.signal(signum, handler)
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)


def run(argv, until, *, env=None, input=None):
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise Failure('Command deadline expired. No guest process was signalled.', 4)
    relay = ProcessSignalRelay()
    process = None
    with relay:
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env=env, start_new_session=True)
            relay.attach(process)
        except OSError as error:
            if relay.received:
                raise Failure('Host command was interrupted before launch completed. Guest state is unconfirmed.', 4) from error
            raise Failure(f'Cannot run {argv[0]}: {error}') from error
        try:
            output, error = process.communicate(input, timeout=remaining)
        except subprocess.TimeoutExpired:
            cleanup_warning = cleanup_process_group(process)
            raise Failure('Host command timed out. Guest state is unconfirmed; no guest stop or kill was requested.' + cleanup_warning, 4)
        except KeyboardInterrupt:
            cleanup_warning = cleanup_process_group(process)
            raise Failure('Host command was interrupted. Guest state is unconfirmed.' + cleanup_warning, 4)
    if relay.received:
        cleanup_warning = cleanup_process_group(process)
        raise Failure('Host command was interrupted. Guest state is unconfirmed.' + cleanup_warning, 4)
    if process.returncode:
        diagnostics = [line for line in output.splitlines() if line in DIAGNOSTICS]
        details = ' '.join(diagnostics) or 'No recognized diagnostic. Inspect locally with doctor or logs; do not publish raw logs.'
        raise Failure(f'{Path(argv[0]).name} failed ({process.returncode}). {details}')
    return output


def host_architecture(until):
    system = platform.system()
    machine = platform.machine()
    if system != 'Darwin':
        return HostArchitecture(system, machine, machine, False)
    try:
        arm64 = run(['/usr/sbin/sysctl', '-in', 'hw.optional.arm64'], until).strip()
    except Failure as error:
        raise Failure('Physical host architecture evidence is inconclusive.', error.code if error.code == 4 else 3) from error
    if arm64 not in {'0', '1'}:
        raise Failure('Physical host architecture evidence is inconclusive.', 3)
    hardware = 'arm64' if arm64 == '1' else 'x86_64'
    if machine != 'x86_64' or hardware != 'arm64':
        return HostArchitecture(system, machine, hardware, False)
    try:
        translated_value = run(['/usr/sbin/sysctl', '-in', 'sysctl.proc_translated'], until).strip()
    except Failure as error:
        raise Failure('Rosetta translation evidence is inconclusive.', error.code if error.code == 4 else 3) from error
    if translated_value not in {'0', '1'}:
        raise Failure('Rosetta translation evidence is inconclusive.', 3)
    return HostArchitecture(system, machine, hardware, translated_value == '1')


def container_runtime_probe():
    contents = (ROOT / 'config/container-runtime-state.sh').read_text()
    if not contents.startswith('#!/bin/bash\n'):
        raise Failure('Reviewed container runtime probe is missing or malformed.', 3)
    return contents.split('\n', 1)[1]


def validate_config(config):
    required = {'version', 'vm', 'lima_home', 'guest_user', 'guest_uid', 'unit'}
    if not isinstance(config, dict) or not required <= config.keys() or config.keys() - required - {'repo', 'runner_id', 'resources', 'shared_with'}:
        raise Failure('Invalid configuration fields.', 2)
    if type(config['version']) is not int or config['version'] not in {VERSION, 2, 3}:
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
    if config['version'] == VERSION and 'resources' in config:
        raise Failure('Legacy configuration cannot contain creation resources.', 2)
    if config['version'] == VERSION and ('repo' in config) != ('runner_id' in config):
        raise Failure('--repo and --runner-id must be supplied together.', 2)
    if config['version'] in {2, 3} and 'repo' not in config:
        raise Failure('Repository profile requires repo.', 2)
    if 'repo' in config:
        if not isinstance(config['repo'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config['repo']):
            raise Failure('Invalid repository.', 2)
        if config['version'] in {2, 3}:
            try:
                if repository_value(config['repo']) != config['repo']:
                    raise ValueError
            except (argparse.ArgumentTypeError, ValueError):
                raise Failure('Repository profile requires a normalized OWNER/REPO.', 2)
    if config['version'] == 3:
        try:
            if not isinstance(config.get('shared_with'), str):
                raise ValueError
            anchor = repository_value(config['shared_with'])
            if anchor != config['shared_with'] or anchor == config['repo']:
                raise ValueError
        except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
            raise Failure('Shared profile requires a different normalized anchor repository.', 2)
        if 'resources' in config or (config['guest_user'], config['guest_uid'], config['unit']) != ('ci', 1001, member_unit(config['repo'])):
            raise Failure('Shared profiles require the derived ci service and cannot resize a VM.', 2)
    elif 'shared_with' in config:
        raise Failure('Only an explicit shared profile can name an anchor.', 2)
    if 'runner_id' in config:
        if type(config['runner_id']) is not int or config['runner_id'] <= 0:
            raise Failure('Invalid runner ID.', 2)
    if 'resources' in config:
        resources = config['resources']
        if not isinstance(resources, dict) or resources.keys() != RESOURCE_LIMITS.keys():
            raise Failure('Invalid creation resources.', 2)
        for key, (low, high) in RESOURCE_LIMITS.items():
            if type(resources[key]) is not int or not low <= resources[key] <= high:
                raise Failure(f'{key} must be an integer from {low} to {high}.', 2)
    return config


def paths():
    home = Path.home()
    return home, home / '.local/share/github-runner-vm', home / '.config/github-runner-vm', home / '.local/bin/ci-vm'


def bundled_setup_reference():
    guide = ROOT / 'docs/setup.md'
    return guide if guide.is_file() and not guide.is_symlink() else None


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


def profile_key(repo):
    slug = re.sub('[^a-z0-9]+', '-', repo).strip('-')[:36]
    return slug + '-' + hashlib.sha256(repo.encode()).hexdigest()[:12]


def automatic_vm_name(repo, lima_home):
    digest = hashlib.sha256(repo.encode()).hexdigest()[:12]
    maximum = DARWIN_UNIX_PATH_BYTES - len(os.fsencode(str(Path(lima_home)))) - LIMA_SOCKET_COMPONENT_RESERVE - 2
    minimum = 'ci-' + digest
    if maximum < len(os.fsencode(minimum)):
        raise Failure('The Lima home is too long for macOS Unix sockets. Choose a shorter absolute --lima-home before provisioning.', 2)
    slug = re.sub('[^a-z0-9]+', '-', repo).strip('-')
    available = max(0, maximum - len(os.fsencode('ci--' + digest)))
    return 'ci-' + (slug[:available] + '-' if available else '') + digest


def validate_lima_socket_path(lima_home, vm):
    instance_path = len(os.fsencode(str(Path(lima_home) / vm)))
    if instance_path + LIMA_SOCKET_COMPONENT_RESERVE + 1 > DARWIN_UNIX_PATH_BYTES:
        raise Failure('The VM name and Lima home exceed the macOS Unix socket path limit. Use a shorter --provision name or --lima-home.', 2)


def member_unit(repo):
    return 'ci-vm-runner@' + profile_key(repo) + '.service'


def unit_bytes(config):
    if config['version'] == 3:
        return (ROOT / 'config/ci-vm-runner@.service').read_bytes().replace(b'@KEY@', profile_key(config['repo']).encode())
    return (ROOT / 'config/ci-vm-runner.service').read_bytes()


def configurations():
    home, _, directory, _ = paths()
    legacy = directory / 'config.json'
    profiles = directory / 'profiles'
    for path in (legacy, profiles):
        safe_path(path, home)
    files = [(legacy, VERSION)] if legacy.exists() else []
    try:
        if profiles.exists():
            if not profiles.is_dir():
                raise Failure('Repository profiles path must be a directory.', 2)
            files.extend((path, 2) for path in sorted(profiles.glob('*.json')))
        result = []
        for path, version in files:
            safe_path(path, home)
            if not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
                raise Failure('Configuration must be a regular file with mode 0600.', 2)
            config = validate_config(strict_json(path.read_text()))
            if (version == VERSION and config['version'] != VERSION or version == 2 and
                    (config['version'] not in {2, 3} or path.stem != profile_key(config['repo']))):
                raise Failure('Configuration version or repository profile filename differs.', 2)
            result.append((path, config))
        identities = {path: vm_identity(config) for path, config in result}
        anchors = {config.get('repo'): (path, config) for path, config in result if config['version'] == 2}
        for path, config in result:
            if config['version'] == 3:
                anchor_path, anchor = anchors.get(config['shared_with'], (None, None))
                if anchor is None or (anchor['guest_user'], anchor['guest_uid'], anchor['unit']) != ('ci', 1001, 'ci-vm-runner.service') or identities[path] != identities[anchor_path]:
                    raise Failure('Shared anchor is missing, unsupported, or names a different physical VM. Preserve the reservation and resolve its anchor.', 2)
        for index, (path, config) in enumerate(result):
            for other_path, other in result[:index]:
                if identities[path] == identities[other_path] and config.get('shared_with', config.get('repo')) != other.get('shared_with', other.get('repo')):
                    raise Failure('Multiple configurations claim the same VM. Resolve the bindings before continuing.', 2)
                if 'repo' in config and 'repo' in other and config['repo'].lower() == other['repo'].lower():
                    raise Failure('Multiple configurations claim the same repository.', 2)
        return result
    except (OSError, ValueError) as error:
        raise Failure(f'Cannot read local configuration. No VM was changed. {error}', 2) from error


def vm_identity(config):
    try:
        path = (Path(config['lima_home']) / config['vm']).resolve()
        missing = []
        while True:
            try:
                info = path.stat()
                return info.st_dev, info.st_ino, tuple(reversed(missing))
            except FileNotFoundError:
                missing.append(path.name)
                path = path.parent
    except (OSError, RuntimeError) as error:
        raise Failure('Cannot resolve the configured VM path.', 2) from error


def load_config(repo=None, legacy=False):
    configs = configurations()
    if not configs:
        guide = bundled_setup_reference()
        if guide is None:
            raise Failure('No installed VM configuration and the bundled setup guide is missing or unsafe. Reinstall github-runner-vm, then retry.', 2)
        raise Failure(f'No installed VM configuration. Follow the current bundled guide: {guide}', 2)
    if repo is not None:
        matches = [config for _, config in configs if config.get('repo', '').lower() == repo]
    elif legacy:
        matches = [config for _, config in configs if config['version'] == VERSION]
    else:
        matches = [config for _, config in configs]
    if len(matches) != 1:
        if not matches:
            raise Failure('No configuration matches the requested repository or legacy selection. Run ci-vm profiles.', 2)
        raise Failure('Multiple configurations exist. Select --repo OWNER/REPO or --legacy. Run ci-vm profiles.', 2)
    selected = matches[0]
    identity = vm_identity(selected)
    members = tuple(config for _, config in configs if vm_identity(config) == identity)
    return Selection(selected, members)


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

PAUSE_MARKER = '''validate_pause_marker() {
  local state=/var/lib/ci-vm marker=/var/lib/ci-vm/paused
  test -d "$state"
  test ! -L "$state"
  test "$(stat -c '%u:%a' "$state")" = 0:755
  test -f "$marker"
  test ! -L "$marker"
  test "$(stat -c '%u:%a' "$marker")" = 0:644
}
set_pause_marker() {
  local state=/var/lib/ci-vm marker=/var/lib/ci-vm/paused temporary=''
  test -d "$state"
  test ! -L "$state"
  test "$(stat -c '%u:%a' "$state")" = 0:755
  if test -e "$marker" || test -L "$marker"; then validate_pause_marker; return; fi
  temporary=$(mktemp "$state/.paused.XXXXXX")
  trap 'rm -f -- "$temporary"' EXIT
  chmod 0644 "$temporary"
  chown 0:0 "$temporary"
  ln -- "$temporary" "$marker"
  rm -- "$temporary"
  temporary=''
  trap - EXIT
  test -f "$marker"
  test ! -L "$marker"
  test "$(stat -c '%u:%a' "$marker")" = 0:644
}
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
    expected_unit = member_unit(config['repo']) if config['version'] == 3 else 'ci-vm-runner.service'
    if (config['guest_user'], config['guest_uid'], config['unit']) != ('ci', 1001, expected_unit):
        raise Failure('Maintenance requires the supplied ci UID 1001 service contract. Adoption remains read-only.', 3)
    script = USER_ENV + '''test -d /var/lib/ci-vm
test ! -L /var/lib/ci-vm
ctl show "$unit" --property=LoadState,DropInPaths,NeedDaemonReload,Transient
fragment=$(ctl show "$unit" --property=FragmentPath --value)
test -n "$fragment"
fragment=$(readlink -f -- "$fragment")
test -n "$fragment"
printf 'FragmentPath=%s\\n' "$fragment"
printf 'UnitHash='
sudo -n sha256sum "$fragment" | cut -d ' ' -f 1
printf 'UnitOwner='
sudo -n stat -c '%u:%a' "$fragment"
printf 'ActualUID=%s\\n' "$(id -u "$user")"
printf 'MarkerOwner='
sudo -n stat -c '%u:%a' /var/lib/ci-vm
'''
    observed = fields(guest(config, script, until))
    expected_hash = hashlib.sha256(unit_bytes(config)).hexdigest()
    expected = {'LoadState': 'loaded', 'FragmentPath': '/etc/systemd/user/' + config['unit'],
                'DropInPaths': '', 'NeedDaemonReload': 'no', 'Transient': 'no', 'UnitHash': expected_hash,
                'UnitOwner': '0:644', 'ActualUID': '1001', 'MarkerOwner': '0:755'}
    if observed != expected:
        raise Failure('Service contract differs or is inconclusive. Maintenance refused; no service migration is automatic.', 3)


def idle(config, until):
    script = USER_ENV + container_runtime_probe() + '''
ctl show "$unit" --property=ActiveState,SubState,MainPID,ControlPID,ControlGroup,Job
printf 'Paused='
if sudo -n test -f /var/lib/ci-vm/paused && sudo -n test ! -L /var/lib/ci-vm/paused; then echo yes; else echo no; fi
printf 'Runners='
ps -eo comm= | awk '$1 == "Runner.Listener" || $1 == "Runner.Worker" {n++} END {print n+0}'
ci_vm_container_runtime_state "$user" "$uid"
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
while IFS= read -r file; do processes=$(cat "$file"); if [ -n "$processes" ]; then echo no; exit; fi; done <<< "$files"
echo yes
CGROUP
fi
'''
    data = fields(guest(config, script, until))
    required = {'ActiveState', 'SubState', 'MainPID', 'ControlPID', 'ControlGroup', 'Job', 'Paused', 'Runners', 'Containers', 'RuntimeDrift', 'Jobs', 'CgroupEmpty'}
    if data.keys() != required or data['Paused'] not in {'yes', 'no'} or data['RuntimeDrift'] not in {'yes', 'no'} or data['CgroupEmpty'] not in {'yes', 'no'}:
        raise Failure('Idle probe is inconclusive.', 3)
    if any(not data[key].isdigit() for key in ('MainPID', 'ControlPID', 'Runners', 'Containers', 'Jobs')):
        raise Failure('Idle probe returned unknown counts.', 3)
    return data['ActiveState'] == 'inactive' and data['SubState'] == 'dead' and data['MainPID'] == data['ControlPID'] == data['Runners'] == data['Containers'] == data['Jobs'] == '0' and data['Job'] in {'', '0'} and data['Paused'] == data['CgroupEmpty'] == 'yes' and data['RuntimeDrift'] == 'no'


def group_contract(selection, until):
    for member in selection.members:
        verify_contract(member, until)
    output = guest(selection, USER_ENV + '''persistent=$(sudo -n find /etc/systemd/user -maxdepth 1 -name 'ci-vm-runner*.service' -printf '%f\\n')
loaded=$(ctl list-units --all --plain --no-legend --no-pager 'ci-vm-runner*')
enabled=$(ctl list-unit-files --no-legend --no-pager 'ci-vm-runner*')
printf '%s\\n%s\\n%s\\n' "$persistent" "$loaded" "$enabled" | awk 'NF {print $1}' | LC_ALL=C sort -u
''', until)
    observed = output.splitlines()
    expected = {member['unit'] for member in selection.members}
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise Failure('Guest runner inventory differs from the complete local VM membership. A member may be pending or orphaned. Preserve profiles and registrations; finish the exact shared setup or inspect unknown units.', 3)


def group_idle(selection, until):
    states = [idle(member, until) for member in selection.members]
    return all(states)


def shared_setup_gate(config, until):
    observed = guest(config, '''set -euo pipefail
if sudo -n test -e /var/lib/ci-vm/shared-setup || sudo -n test -L /var/lib/ci-vm/shared-setup; then echo SharedSetup=active; else echo SharedSetup=clear; fi
''', until)
    if observed not in {'SharedSetup=active\n', 'SharedSetup=clear\n'}:
        raise Failure('Shared setup gate state is inconclusive.', 3)
    return observed == 'SharedSetup=active\n'


def complete_setup(config, until):
    if shared_setup_gate(config, until):
        raise Failure("Shared setup is unfinished. Keep the VM paused, complete the reserved member's reviewed preparation, and run its selected profile's automatic register transaction. Do not delete the gate or profile.", 3)


def affected_repositories(selection):
    return tuple(member.get('repo', 'legacy') for member in selection.members)


def mutation_scope(selection, all_repos):
    if len(selection.members) > 1 and not all_repos:
        raise Failure('This VM is shared by ' + ', '.join(affected_repositories(selection)) + '. Repeat with --all-repos only when all affected repositories are approved.', 2)


def guest_mutation(config, script, until, *args):
    locked = 'set -euo pipefail\nsudo -n flock -n /var/lib/ci-vm/operation.lock bash -s -- "$@" <<\'CI_VM_MUTATION\'\n' + script + '\nCI_VM_MUTATION\n'
    return guest(config, locked, until, *args)


def maintain(config, command, until, all_repos=False):
    mutation_scope(config, all_repos)
    state = vm_state(config, until)
    if state == 'Stopped' and command == 'resume':
        raise Failure('Resume does not boot an unverified VM. Explicitly start your known existing VM after reviewing its configuration, then run doctor and resume.', 3)
    if state != 'Running':
        raise Failure(f'Maintenance requires a running existing VM; found {state}.', 3)
    if len(config.members) > 1:
        print('Affected repositories: ' + ', '.join(affected_repositories(config)), flush=True)
    try:
        group_contract(config, until)
    except Failure:
        if command == 'pause' and len(config.members) > 1:
            guest_mutation(config, USER_ENV + PAUSE_MARKER + 'set_pause_marker\n', until)
            raise Failure('Pause requested, but complete shared membership is unverified. Marker remains set. Finish the pending shared setup before claiming idle.', 4)
        raise
    if command in {'resume', 'restart'}:
        ensure_no_package_work(config, until)
        complete_setup(config, until)
    if command == 'resume':
        if len(config.members) > 1 and not group_idle(config, until):
            raise Failure('Shared resume requires every member paused and idle, with no unknown runner processes. Let any active listeners drain first.', 3)
        units = [member['unit'] for member in config.members]
        script = USER_ENV + PAUSE_MARKER + '''test ! -e /var/lib/ci-vm/shared-setup
test ! -L /var/lib/ci-vm/shared-setup
test ! -e /var/lib/ci-vm/package-maintenance
test ! -L /var/lib/ci-vm/package-maintenance
validate_pause_marker
shift 3
for member in "$@"; do test "$(ctl is-enabled "$member")" = enabled; done
sudo -n rm -f -- /var/lib/ci-vm/paused
trap set_pause_marker ERR
for member in "$@"; do ctl start --no-block "$member"; done
'''
        try:
            guest_mutation(config, script, until, *units)
        except Failure as error:
            try:
                guest_mutation(config, USER_ENV + PAUSE_MARKER + 'set_pause_marker\n', deadline(5))
            except Failure:
                raise Failure('Partial resume is unconfirmed and restoring the pause marker could not be verified. Inspect all affected runners. No listener was signalled.', error.code) from error
            raise Failure('Resume did not complete. Pause marker restored; already-started listeners drain naturally. Inspect every affected runner.', error.code) from error
        print('Resume requested. Run status and verify the exact runner in GitHub.')
        return
    if command == 'restart':
        reject_inherited_config(config)
        if not group_idle(config, until):
            raise Failure('Restart requires an already paused, inactive runner, no containers, no runner processes, and no systemd jobs.', 3)
        group_contract(config, until)
        if not group_idle(config, until):
            raise Failure('Runner state changed. Restart refused.', 3)
        ensure_no_package_work(config, until)
        complete_setup(config, until)
        lima(config, ['stop', config['vm']], until)
        reject_inherited_config(config)
        lima(config, ['start', '--tty=false', config['vm']], until)
        group_contract(config, until)
        if not group_idle(config, until):
            raise Failure('VM restarted but paused idle state could not be verified.', 3)
        print('VM restarted. Runner remains paused; resume explicitly when ready.')
        return
    guest_mutation(config, USER_ENV + PAUSE_MARKER + 'set_pause_marker\n', until)
    while time.monotonic() < until:
        if group_idle(config, until):
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
    if isinstance(config, Selection) and len(config.members) > 1:
        print('Shared VM repositories: ' + ', '.join(affected_repositories(config)))
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
    if 'runner_id' in config:
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
    try:
        architecture = host_architecture(until)
        print('Host: ' + architecture.display())
    except Failure as error:
        architecture = None
        print('Host: architecture evidence inconclusive')
        failures.append(str(error))
    if architecture is not None and not architecture.supported:
        failures.append('Host is not macOS Apple Silicon.')
    print(lima(config, ['--version'], until).strip())
    try:
        group_contract(config, until) if isinstance(config, Selection) else verify_contract(config, until)
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
    return '#!/bin/sh\nexec ' + shlex.quote(interpreter) + ' ' + shlex.quote(str(module)) + ' "$@"\n'


def recognized_launcher(contents, module):
    lines = contents.splitlines()
    if len(lines) != 2:
        return False
    try:
        words = shlex.split(lines[1])
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


def cleanup_failed_provision_reservation(config, reservation, until):
    path, exact_bytes = reservation
    try:
        if vm_state(config, until) != 'Absent':
            return False
        instance = Path(config['lima_home']) / config['vm']
        if instance.exists() or instance.is_symlink():
            return False
        home, _, _, _ = paths()
        safe_path(path, home)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
            return False
        if path.read_bytes() != exact_bytes:
            return False
        path.unlink()
        return True
    except (Failure, OSError):
        return False


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
    provisioning = args.provision is not None
    if provisioning and not args.provision and args.repo is None:
        raise Failure('Supply --repo OWNER/REPO for an automatic VM name, or --provision NAME for legacy setup.', 2)
    config_path = directory / 'profiles' / (profile_key(args.repo) + '.json') if args.repo else directory / 'config.json'
    safe_path(config_path, home)
    configs = configurations()
    if args.repo and args.adopt:
        legacy = next((path for path, config in configs if config['version'] == VERSION and config.get('repo', '').lower() == args.repo), None)
        if legacy is not None:
            config_path = legacy
    existing = next((config for path, config in configs if path == config_path), None)
    sharing = args.share_with is not None
    shared_anchor = None
    if sharing:
        if not args.repo or args.repo == args.share_with or any(getattr(args, key) is not None for key in ('lima_home', 'guest_user', 'guest_uid', 'unit', 'cpus', 'memory_gib', 'disk_gib')) or args.yes_create_vm:
            raise Failure('--share-with requires a different --repo and cannot override VM identity or creation resources.', 2)
        shared_anchor = load_config(args.share_with)
        if shared_anchor['version'] != 2 or (shared_anchor['guest_user'], shared_anchor['guest_uid'], shared_anchor['unit']) != ('ci', 1001, 'ci-vm-runner.service'):
            raise Failure('--share-with requires a supported v2 repository anchor, not a legacy or shared member.', 2)
    elif existing and existing['version'] == 3:
        raise Failure('A shared reservation can only be reinstalled with its exact --share-with anchor.', 2)
    lima_home = str(Path(args.lima_home).expanduser().absolute()) if args.lima_home else (existing['lima_home'] if existing else str(home / ('.lima' if args.adopt else '.local/share/github-runner-vm/lima')))
    if sharing:
        lima_home = shared_anchor['lima_home']
    vm = shared_anchor['vm'] if sharing else args.adopt or args.provision or (existing['vm'] if existing else automatic_vm_name(args.repo, lima_home))
    config = dict(version=existing['version'] if existing else 2 if args.repo else VERSION, vm=vm, lima_home=lima_home,
                  guest_user=args.guest_user or (existing['guest_user'] if existing else 'ci'),
                  guest_uid=args.guest_uid if args.guest_uid is not None else (existing['guest_uid'] if existing else 1001),
                  unit=args.unit or (existing['unit'] if existing else 'ci-vm-runner.service'))
    if sharing:
        config.update(version=3, shared_with=args.share_with, unit=member_unit(args.repo))
    if args.repo is not None:
        config['repo'] = existing['repo'] if existing and existing['version'] == VERSION else args.repo
    if args.runner_id is not None:
        config['runner_id'] = args.runner_id
    elif existing and 'runner_id' in existing:
        config['runner_id'] = existing['runner_id']
        if 'repo' in existing:
            config['repo'] = existing['repo']
    overrides = {key: getattr(args, key) for key in RESOURCE_DEFAULTS if getattr(args, key) is not None}
    if overrides and (not provisioning or not args.repo):
        raise Failure('Resource options require a new --repo profile with --provision. Adoption never resizes a VM.', 2)
    if args.repo and provisioning:
        config['resources'] = {**RESOURCE_DEFAULTS, **overrides}
    elif existing and 'resources' in existing:
        config['resources'] = existing['resources']
    validate_config(config)
    if existing and existing != config:
        raise Failure('Existing configuration differs. No adoption or overwrite performed.', 2)
    for path, other in configs:
        shared_binding = (sharing or existing is not None) and other.get('shared_with', other.get('repo')) == config.get('shared_with', config.get('repo'))
        if path != config_path and (vm_identity(config) == vm_identity(other) and not shared_binding or args.repo and other.get('repo', '').lower() == args.repo):
            raise Failure('The repository or physical VM is already bound to another configuration. No binding was changed.', 2)
    startup_updates = shell_updates(home) if args.configure_shell else []
    until = deadline(args.timeout)
    if sharing and not existing:
        if vm_state(shared_anchor, until) != 'Running':
            raise Failure('Shared attachment requires a Running VM. Inspect and pause the existing group first; no VM was started.', 3)
        complete_setup(shared_anchor, until)
        ensure_no_package_work(shared_anchor, until)
        group_contract(shared_anchor, until)
        if not group_idle(shared_anchor, until):
            raise Failure('Pause the complete existing VM group before attaching another repository.', 3)
        group_contract(shared_anchor, until)
        if not group_idle(shared_anchor, until):
            raise Failure('VM state changed before shared attachment. No reservation was written.', 3)
    state = vm_state(config, until) if not sharing else 'Running'
    if args.adopt and state == 'Absent':
        raise Failure('The named VM does not exist. Adoption never provisions.', 2)
    if provisioning:
        if (config['guest_user'], config['guest_uid'], config['unit']) != ('ci', 1001, 'ci-vm-runner.service'):
            raise Failure('Provisioning uses the supplied ci UID 1001 service contract. Custom identities are adoption-only.', 2)
        if not args.yes_create_vm:
            raise Failure('Provisioning requires --yes-create-vm.', 2)
        architecture = host_architecture(until)
        if not architecture.supported:
            raise Failure('Provisioning requires an Apple Silicon Mac.', 2)
        validate_lima_socket_path(lima_home, vm)
        if existing or state != 'Absent' or (Path(lima_home) / vm).exists() or (Path(lima_home) / vm).is_symlink():
            raise Failure('VM already exists. Provisioning never replaces or recreates it.', 2)
        version = lima(config, ['--version'], until)
        match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (2, 2, 0):
            raise Failure('Provisioning requires Lima 2.2.0 or newer.', 2)
        reject_inherited_config(config)
    sources = {name: (ROOT / name).read_bytes() for name in INSTALL_FILES}
    module_bytes = Path(__file__).read_bytes()
    for path in (share / 'ci_vm.py', *(share / name for name in sources)):
        safe_path(path, home)
    owned_directory(share, 0o755)
    owned_directory(directory, 0o700)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    for path in (share / 'ci_vm.py', owner_file):
        safe_path(path, home)
    for name, contents in sources.items():
        target = share / name
        safe_path(target, home)
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_path(target, home)
        atomic_write(target, contents, 0o644)
    atomic_write(share / 'ci_vm.py', module_bytes, 0o644)
    atomic_write(owner_file, OWNER.encode(), 0o600)
    reservation = None
    if not existing:
        config_path.parent.mkdir(mode=0o700, exist_ok=True)
        safe_path(config_path, home)
        profile_bytes = (json.dumps(config, indent=2) + '\n').encode()
        atomic_write(config_path, profile_bytes, 0o600)
        if provisioning and args.repo:
            reservation = (config_path, profile_bytes)
    atomic_write(launcher, launch_content.encode(), 0o755)
    for path, original, data, mode in startup_updates:
        safe_path(path, home)
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_path(path, home)
        if (path.read_bytes() if path.exists() else None) != original:
            raise Failure(f'Shell startup file changed during install: {path}. Rerun after reviewing it.', 2)
        atomic_write(path, data, mode)
        print(f'Added PATH block to {path}', flush=True)
    if provisioning:
        print('Creating the explicitly requested VM. A timeout leaves its state intact for inspection.', flush=True)
        try:
            reject_inherited_config(config)
            resources = config.get('resources', RESOURCE_DEFAULTS)
            lima(config, ['start', '--tty=false', '--name', vm, '--cpus', str(resources['cpus']),
                          '--memory', str(resources['memory_gib']), '--disk', str(resources['disk_gib']), str(share / 'config/lima.yaml')], until)
        except (Failure, OSError) as error:
            if reservation is not None and (not isinstance(error, Failure) or error.code != 4):
                cleanup_failed_provision_reservation(config, reservation, until)
            raise
    setup_command = f'setup {config["repo"]}' if 'repo' in config else '--legacy setup OWNER/REPO'
    selector = f'--repo {config["repo"]}' if 'repo' in config else '--legacy'
    print(f'Installed ci-vm. Next: "$HOME/.local/bin/ci-vm" {setup_command}')
    if args.configure_shell:
        print(f'Continue now with "$HOME/.local/bin/ci-vm" {selector} status. ci-vm will be on PATH in new Bash and Zsh sessions.')
    else:
        print(f'No shell startup files were changed. Continue with "$HOME/.local/bin/ci-vm" {selector} status.')
    if sharing:
        print('Shared profile reserved. Existing registrations were not changed. Keep every repository paused until the exact member preparation and authenticated registration are finished.')
        print('Affected repositories: ' + ', '.join(dict.fromkeys((*affected_repositories(shared_anchor), args.repo))))
    else:
        print('Adoption does not change the VM or its runner registration.' if args.adopt else 'VM provision request completed. Register the runner through authenticated host gh.')
    print('The setup command prints a read-only guide. It does not change or assume the registration of an adopted VM.' if args.adopt else 'The setup command prints a read-only guide. Registration and a verified job are still required for a new runner.')
    print(f'Full guide: {share / "docs/setup.md"}')
    print(f'Agent runbook: {share / "docs/llm-setup.md"}')


def timeout_value(value):
    try:
        number = float(value)
        if not 0 < number <= 3600:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError('timeout must be greater than zero and at most 3600 seconds')


APT_OPTIONS = ('--no-remove', '--no-install-recommends',
               '-o', 'APT::Get::AllowUnauthenticated=false', '-o', 'APT::Get::allow-downgrades=false',
               '-o', 'APT::Get::allow-change-held-packages=false', '-o', 'APT::Get::allow-remove-essential=false',
               '-o', 'APT::Get::force-yes=false', '-o', 'Acquire::AllowInsecureRepositories=false',
               '-o', 'Acquire::AllowDowngradeToInsecureRepositories=false', '-o', 'Acquire::AllowWeakRepositories=false')
PACKAGE_PROBE = '''set -euo pipefail
export LC_ALL=C
unset APT_CONFIG
test "$(uname -m)" = aarch64
. /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04
shift 3
test -z "$(dpkg --audit)"
for request in "$@"; do
  name=${request%%=*}
  actual=$(apt-cache --no-all-versions show -- "$name" | awk '$1 == "Package:" {print $2}' | sort -u)
  test "$actual" = "$name"
  installed=$(dpkg-query -W -f='${Status}\\t${Version}' -- "$name" 2>/dev/null) || installed=''
  case "$installed" in
    'install ok installed'*) version=${installed##*$'\\t'};;
    ''|'deinstall ok config-files'*) version=-;;
    *) exit 3;;
  esac
  printf 'P\\t%s\\t%s\\n' "$name" "$version"
done
holds=$(apt-mark showhold)
while IFS= read -r name; do
  if [ -n "$name" ]; then printf 'H\\t%s\\n' "$name"; fi
done <<< "$holds"
if test -f /var/run/reboot-required; then printf 'R\\tyes\\n'; else printf 'R\\tno\\n'; fi
printf 'PLAN\\n'
''' + shlex.join(['apt-get', '--simulate', '-o', 'Debug::NoLocking=true', *APT_OPTIONS, 'install', '--']) + ' "$@"\n'


def package_gate(config, until):
    script = '''set -euo pipefail
sudo -n bash -c 'if [ -e /var/lib/ci-vm/package-maintenance ] || [ -L /var/lib/ci-vm/package-maintenance ]; then echo PackageGate=active; else echo PackageGate=clear; fi'
'''
    observed = guest(config, script, until)
    if observed not in {'PackageGate=active\n', 'PackageGate=clear\n'}:
        raise Failure('Package-maintenance gate is inconclusive. Mutation refused.', 3)
    return observed == 'PackageGate=active\n'


def ensure_no_package_work(config, until):
    if package_gate(config, until):
        raise Failure('A package-maintenance gate remains set. Inspect package processes, locks, and installed state before reviewed recovery. Resume and restart are refused.', 3)


def package_probe(config, requests, until):
    return package_observation(guest(config, PACKAGE_PROBE, until, *(request.argument() for request in requests)), requests)


def package_argument(value):
    try:
        return package_request(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def print_receipt(receipt, as_json):
    if as_json:
        print(json.dumps(receipt, sort_keys=True))
    else:
        lines = [f'{key}: {json.dumps(value, sort_keys=True)}' for key, value in receipt.items() if key not in {'operation', 'outcome'}]
        print_sections(receipt['operation'], receipt['outcome'], [('Result', lines)])


def packages(args):
    receipt = {'operation': 'packages', 'outcome': 'unverified', 'requested': [request.argument() for request in args.packages],
               'paused': 'unverified', 'source_trust': 'Configured APT sources require separate review; this command does not certify provenance.'}
    code = 0
    try:
        if args.yes and not args.apply:
            raise Failure('--yes requires --apply.', 2)
        if not 1 <= len(args.packages) <= 64 or len({request.name for request in args.packages}) != len(args.packages):
            raise Failure('Request 1..64 distinct package names.', 2)
        config = load_config(args.selected_repo, args.legacy)
        if args.apply:
            mutation_scope(config, args.all_repos)
        receipt['repo'] = config.get('repo')
        receipt['vm'] = config['vm']
        receipt['affected_repositories'] = affected_repositories(config)
        until = deadline(args.timeout)
        if vm_state(config, until) != 'Running':
            raise Failure('Package inspection requires an already running VM. No VM was started.', 3)
        receipt['maintenance_gate'] = 'active' if package_gate(config, until) else 'clear'
        if args.apply:
            complete_setup(config, until)
            if receipt['maintenance_gate'] != 'clear':
                raise Failure('Package-maintenance gate remains set. Inspect before reviewed recovery; installation refused.', 3)
            group_contract(config, until)
            if not group_idle(config, until):
                raise Failure('Package installation requires an already paused, completely idle runner.', 3)
            receipt['paused'] = True
        plan = package_probe(config, args.packages, until)
        receipt.update(installed=plan['installed'], transaction=plan['changes'], reboot_required=plan['reboot_required'])
        receipt['effects'] = 'APT can run package maintainer scripts as root and restart services. The runner must remain paused.'
        if not args.apply:
            receipt['outcome'] = 'preview'
        else:
            if not args.yes:
                if not sys.stdin.isatty():
                    raise Failure('Noninteractive package installation requires --apply --yes for this exact request.', 2)
                print(f'VM {config["vm"]}; request {receipt["requested"]}; transaction {plan["changes"]}', file=sys.stderr)
                print(receipt['effects'], file=sys.stderr)
                print('Apply this exact package transaction? Type yes: ', end='', file=sys.stderr, flush=True)
                if sys.stdin.readline().strip() != 'yes':
                    raise Failure('Package installation was not confirmed.', 2)
            pinned = [PackageRequest(name, version) for name, version in sorted(plan['intended'].items())]
            current = package_probe(config, args.packages, until)
            if current != plan:
                raise Failure('Package transaction changed after preview. Review a new request.', 3)
            pinned_plan = package_probe(config, pinned, until)
            if pinned_plan['changes'] != plan['changes'] or pinned_plan['intended'] != plan['intended']:
                raise Failure('Pinned package transaction differs. Installation refused.', 3)
            group_contract(config, until)
            if not group_idle(config, until):
                receipt['paused'] = 'unverified'
                raise Failure('Runner state changed. Package installation refused.', 3)
            ensure_no_package_work(config, until)
            complete_setup(config, until)
            if plan['changes']:
                receipt['paused'] = 'unverified'
                receipt['maintenance_gate'] = 'unverified'
                gate = guest_mutation(config, 'set -euo pipefail\ntest ! -e /var/lib/ci-vm/shared-setup\ntest ! -L /var/lib/ci-vm/shared-setup\nsudo -n mkdir -m 700 -- /var/lib/ci-vm/package-maintenance\nsudo -n stat -c "PackageGate=%u:%a" /var/lib/ci-vm/package-maintenance\n', until)
                if gate != 'PackageGate=0:700\n':
                    raise Failure('Package-maintenance gate ownership could not be verified. Installation refused.', 3)
                receipt['maintenance_gate'] = 'active'
                script = 'set -euo pipefail\ntest ! -e /var/lib/ci-vm/shared-setup\ntest ! -L /var/lib/ci-vm/shared-setup\nshift 3\n' + shlex.join([
                    'sudo', '-n', 'env', '-u', 'APT_CONFIG', 'LC_ALL=C', 'DEBIAN_FRONTEND=noninteractive', 'NEEDRESTART_MODE=l',
                    'apt-get', *APT_OPTIONS, '--assume-yes', 'install', '--']) + ' "$@"\n'
                try:
                    guest_mutation(config, script, until, *(request.argument() for request in pinned))
                except Failure as error:
                    receipt['outcome'] = 'uncertain' if error.code == 4 else 'failed'
                    receipt['paused'] = 'unverified'
                    raise Failure('APT did not complete with verified success. Guest work may still be running. Inspect package state and locks before retrying; no lock or pause marker was cleared.', error.code) from error
            observed = package_probe(config, pinned, until)
            receipt['installed'] = observed['installed']
            receipt['reboot_required'] = observed['reboot_required']
            if observed['installed'] != plan['intended'] or observed['changes']:
                raise Failure('Installed versions do not match the approved transaction. Inspect before retrying.', 3)
            group_contract(config, until)
            if not group_idle(config, until):
                receipt['paused'] = 'unverified'
                raise Failure('Post-install paused idle state could not be verified.', 3)
            receipt['paused'] = True
            if plan['changes']:
                guest_mutation(config, 'set -euo pipefail\ntest ! -e /var/lib/ci-vm/shared-setup\ntest ! -L /var/lib/ci-vm/shared-setup\ntest "$(sudo -n stat -c "%u:%a" /var/lib/ci-vm/package-maintenance)" = 0:700\nsudo -n rmdir -- /var/lib/ci-vm/package-maintenance\n', until)
                if package_gate(config, until):
                    raise Failure('Package-maintenance gate remains active after verification.', 3)
                receipt['maintenance_gate'] = 'clear'
            receipt['outcome'] = 'installed'
    except (Failure, ValueError, OSError) as error:
        code = error.code if isinstance(error, Failure) else 3
        if receipt['outcome'] == 'unverified':
            receipt['outcome'] = 'failed'
        receipt['error'] = str(error) if isinstance(error, (Failure, ValueError)) else 'Local operation failed.'
        receipt['next'] = 'Review configured APT sources and package indexes separately. Indexes were not refreshed and the runner was not resumed.'
    print_receipt(receipt, args.json)
    return code


def positive_id(value):
    try:
        number = int(value)
        if not 0 < number <= 2 ** 63 - 1:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError('use a positive integer ID')


def verify_run(args):
    receipt = {'operation': 'verify-run', 'outcome': 'unverified', 'run_id': args.run_id}
    code = 3
    try:
        if not re.fullmatch(r'[0-9a-fA-F]{40}', args.expect_sha) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', args.expect_event):
            raise Failure('Supply a full commit SHA and exact event name.', 2)
        if not 1 <= len(args.jobs) <= 100 or len(set(args.jobs)) != len(args.jobs) or any(not job.strip() or len(job) > 300 or any(ord(char) < 32 or ord(char) == 127 for char in job) for job in args.jobs):
            raise Failure('Supply 1..100 distinct, nonempty exact job names without control characters.', 2)
        config = load_config(args.selected_repo, args.legacy)
        if 'repo' not in config:
            raise Failure('Run verification requires a profile with an exact repository identity.', 2)
        if 'runner_id' not in config:
            raise Failure('Run verification requires a registered profile with a persisted runner ID.', 2)
        if config['runner_id'] != args.expect_runner_id:
            raise Failure('Expected runner ID differs from the recorded profile identity.', 2)
        repo = config['repo'].lower()
        receipt['repo'] = repo
        until = deadline(args.timeout)
        endpoint = f'repos/{repo}/actions/runs/{args.run_id}'
        expected = {'repo': repo, 'run_id': args.run_id, 'sha': args.expect_sha.lower(), 'event': args.expect_event,
                    'runner_id': config['runner_id'], 'jobs': args.jobs}
        first = strict_json(run(['gh', 'api', '--hostname', 'github.com', endpoint], until))
        assess_run(first, [], expected)
        attempt = first['run_attempt']
        jobs = []
        total = None
        for page in range(1, 51):
            response = strict_json(run(['gh', 'api', '--hostname', 'github.com', f'{endpoint}/attempts/{attempt}/jobs?per_page=100&page={page}'], until))
            if not isinstance(response, dict) or type(response.get('total_count')) is not int or not 0 <= response['total_count'] <= 5000 or not isinstance(response.get('jobs'), list) or len(response['jobs']) > 100:
                raise Failure('GitHub jobs page is incomplete or invalid.', 3)
            if total is None:
                total = response['total_count']
            if total != response['total_count'] or len(jobs) + len(response['jobs']) > total:
                raise Failure('GitHub job count changed during pagination.', 3)
            jobs.extend(response['jobs'])
            if len(jobs) == total:
                break
            if len(response['jobs']) != 100:
                raise Failure('GitHub jobs pagination ended before the declared count.', 3)
        else:
            raise Failure('GitHub jobs exceed the verification page limit.', 3)
        result = assess_run(first, jobs, expected)
        current = strict_json(run(['gh', 'api', '--hostname', 'github.com', endpoint], until))
        assess_run(current, [], expected)
        if any(current[key] != first[key] for key in ('id', 'head_sha', 'event', 'run_attempt', 'status', 'conclusion')):
            raise Failure('GitHub run changed during verification. Re-run verification for the current attempt.', 3)
        receipt.update(result)
        receipt['outcome'] = 'verified' if result['verified'] else 'unverified'
        code = 0 if result['verified'] else 3
    except (Failure, ValueError, TypeError, KeyError, OSError) as error:
        code = error.code if isinstance(error, Failure) else 3
        receipt['error'] = str(error) if isinstance(error, (Failure, ValueError)) else 'GitHub evidence is invalid or incomplete.'
    print_receipt(receipt, args.json)
    return code


def repository_value(value):
    value = value.removeprefix('https://github.com/').removesuffix('/').removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise argparse.ArgumentTypeError('use OWNER/REPO or https://github.com/OWNER/REPO')
    return value.lower()


def print_sections(title, subtitle, sections):
    color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    start, end = ('\033[1;36m', '\033[0m') if color else ('', '')
    print(f'\n  {start}{title}{end}\n  {subtitle}')
    for heading, lines in sections:
        print(f'\n  {start}{heading}{end}')
        for line in lines:
            print(f'    {line}')
    print()


def overview():
    sections = (
        ('Connect a repository', ('ci-vm setup OWNER/REPO', 'Print the next steps. Nothing is changed.')),
        ('Inspect', tuple(f'ci-vm {name:<9} {description}' for name, description in COMMANDS[1:4])),
        ('Maintain', tuple(f'ci-vm {name:<9} {description}' for name, description in COMMANDS[4:7])),
        ('Select a repository', ('ci-vm profiles', 'ci-vm --repo OWNER/REPO status', 'With multiple configurations, select --repo OWNER/REPO or --legacy.')),
        ('Agent operations', ('ci-vm --repo OWNER/REPO register', 'ci-vm --repo OWNER/REPO packages PACKAGE', 'ci-vm --repo OWNER/REPO verify-run --help')),
        ('Help', ('ci-vm --help', 'GitHub schedules jobs. The VM runs selected Linux ARM64 jobs.')),
    )
    print_sections('ci-vm', 'GitHub Actions on your Mac, inside a Linux VM.', sections)


def setup_guide(config, repo):
    if 'repo' in config and config['repo'].lower() != repo.lower():
        raise Failure('Requested repository differs from the configured runner. No configuration was changed.', 2)
    managed = (config['guest_user'], config['guest_uid'], config['unit']) == ('ci', 1001, member_unit(repo) if config['version'] == 3 else 'ci-vm-runner.service')
    guide = ROOT / 'docs' / ('setup.md' if managed else 'maintenance.md')
    if not guide.is_file():
        raise Failure('Bundled guide is missing. Reinstall from a complete reviewed source copy without changing the existing VM.', 2)
    identity = f'VM {config["vm"]} | user {config["guest_user"]} | UID {config["guest_uid"]} | unit {config["unit"]}'
    subtitle = 'Read-only guide. No commands below were executed; setup is not verified.'
    command = f'ci-vm --repo {repo}' if config['version'] == 2 or 'repo' in config else 'ci-vm --legacy'
    register_command = command + ' register' + (' --manual-token ' + repo if config['version'] == VERSION else '') + (' --all-repos' if isinstance(config, Selection) and len(config.members) > 1 else '')
    resume_command = command + ' resume' + (' --all-repos' if isinstance(config, Selection) and len(config.members) > 1 else '')
    if isinstance(config, Selection) and len(config.members) > 1:
        print('Shared VM repositories: ' + ', '.join(affected_repositories(config)))
        print('Registration, pause, resume, restart, and package application require --all-repos and affect every repository.')
        print('Jobs can run concurrently and share the CI account, packages, Docker, ports, caches, resources, and trust.')
    if config['version'] == 3:
        key = profile_key(repo)
        print_sections('Prepare the reserved member', 'Keep the whole VM paused through registration readiness.', (
            ('Exact member identity', (f'RUNNER_KEY={key}', f'Unit {config["unit"]}', f'Runner directory /home/ci/runners/{key}', f'Work directory /home/ci/work/{key}')),
            ('Reviewed guest helper', (str(ROOT / 'config/prepare-shared-runner.sh'), str(ROOT / 'config/container-runtime-state.sh'), str(ROOT / 'config/ci-vm-runner@.service'), 'The agent prepares the reserved member with the reviewed helper.', 'register stages these exact files, enables the inactive unit, and finishes only this matching gate.')),
        ))
    if not managed:
        sections = (
            ('Inspect the adopted runner from the Mac', (
                identity,
                command + ' status',
                command + ' doctor',
                'A different service contract may make doctor report unsupported maintenance.',
            )),
            ('Preserve the existing setup', (
                'Keep its existing registration and service. Review any migration separately.',
                'Do not install another listener or apply the new-VM registration procedure.',
                f'Confirm the existing runner belongs to {repo} before changing workflows.',
            )),
            ('Adoption guide', (
                str(guide) + '#adopt-without-changing-existing-behavior',
            )),
        )
        print_sections(f'Inspect adoption for {repo}', subtitle, sections)
        return
    service_section = (
        '3. Verify, then resume the runner service',
        ('register already enabled the exact service without starting it; the VM remains paused.',
         command + ' doctor', resume_command, command + ' status'),
    ) if config['version'] in {2, 3} else (
        '3. Review the runner service from the Mac',
        ('Only for a newly provisioned VM or an independently verified service contract.',
         'Follow the full guide before resuming a legacy registration.', resume_command,
         command + ' status', command + ' doctor'),
    )
    registration_lines = (
        'Already registered? Preserve that registration and skip this step.',
        'Use a trusted private repository. Never run public fork jobs automatically.',
        register_command,
        'Legacy profiles require this uncaptured manual fallback. Automatic token retrieval and completion are unavailable.',
        'Never paste a runner token into chat.',
    ) if config['version'] == VERSION else (
        'Existing registration files are recovery input. Preserve them and rerun register so exact GitHub identity, profile state, service enablement, and setup gates are reconciled.',
        'Use a trusted private repository. Never run public fork jobs automatically.',
        register_command,
        'The normal repository-profile path uses authenticated host gh, verifies the exact runner, enables its inactive service, and leaves the VM paused.',
        'If gh needs authentication, an agent runs gh auth login --hostname github.com --web; approve access in the browser, then retry.',
        'Never paste a runner token into chat. --manual-token is troubleshooting only.',
    )
    sections = (
        ('1. Check the selected VM', (
            identity,
            command + ' status',
            command + ' doctor',
            'A stopped VM stays stopped. Review its configuration before an explicit start.',
        )),
        ('2. Register only a new runner', registration_lines),
        service_section,
        ('4. Verify an approved GitHub Actions job', (
            'Review examples/smoke.yml and publish it in your target repository only after approval.',
            'For the guide\'s default labels, use runs-on: [self-hosted, Linux, ARM64, spare-mac].',
            'Dispatch only after approval. Verify the event, commit, job result, and exact runner ID/name.',
            'An online runner or a successful doctor check is not proof that a job ran.',
        )),
        ('5. Route your real CI jobs', (
            'Review the target workflow before changing its runs-on labels.',
            'Keep existing push/PR events, filters, permissions, checks, and dependencies.',
            'Keep Xcode and macOS jobs on macOS. Confirm Linux ARM64 dependencies.',
            'GitHub continues to trigger workflows; matching jobs execute inside this VM.',
        )),
        ('Full registration and verification guide', (str(guide),)),
        ('Agent runbook', (str(ROOT / 'docs/llm-setup.md'), command + ' packages PACKAGE', command + ' verify-run --help', 'Preview dependencies, then verify the exact Actions run. This guide executes neither.')),
    )
    print_sections(f'Connect {repo}', subtitle, sections)


def registration_paths(config):
    if config['version'] == 3:
        key = profile_key(config['repo'])
        return '/home/ci/runners/' + key, '/home/ci/work/' + key
    return '/home/ci/actions-runner', '/home/ci/work/actions'


def registration_target(config, repo=None):
    target_repo = config.get('repo') or repo
    if target_repo is None:
        raise Failure('Legacy registration requires OWNER/REPO.', 2)
    if config.get('repo') and repo and config['repo'] != repo:
        raise Failure('Registration repository differs from the selected profile.', 2)
    runner_directory, work_directory = registration_paths(config)
    leaf = re.sub('[^a-z0-9]+', '-', target_repo.split('/', 1)[1]).strip('-')
    identity = hashlib.sha256((config['lima_home'] + '\0' + config['vm']).encode()).hexdigest()[:8]
    return RegistrationTarget(target_repo, 'https://github.com/' + target_repo,
                              leaf + '-' + identity + '-arm64', ('spare-mac', leaf + '-ci'),
                              runner_directory, work_directory, config['unit'],
                              profile_key(target_repo) if config['version'] == 3 else None)


def github_environment():
    environment = dict(os.environ)
    for name in ('GH_DEBUG', 'GH_TRACE', 'GH_FORCE_TTY', 'DEBUG'):
        environment.pop(name, None)
    environment['GH_PAGER'] = 'cat'
    environment['NO_COLOR'] = '1'
    return environment


def github_auth(until):
    if shutil.which('gh') is None:
        raise Failure('GitHub CLI is required for unattended registration. Install gh under the approved host setup, then retry.', 2)
    try:
        run(['gh', 'auth', 'status', '--hostname', 'github.com', '--active'], until, env=github_environment())
    except Failure as error:
        if error.code == 4:
            raise
        raise Failure('GitHub CLI is not authenticated. Run gh auth login --hostname github.com --web in a user-visible terminal, approve access in the browser, then retry.', 3) from error


def parse_remote_runner(value):
    try:
        labels = value['labels']
        result = RemoteRegistration(value['id'], value['name'], value['status'], value['busy'],
                                    tuple(label['name'] for label in labels))
        if (type(result.runner_id) is not int or result.runner_id <= 0 or not isinstance(result.name, str) or
                not result.name or result.status not in {'online', 'offline'} or type(result.busy) is not bool or
                not isinstance(labels, list) or any(not isinstance(label, dict) or 'name' not in label or
                                                   not isinstance(label['name'], str) for label in labels)):
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError):
        raise Failure('GitHub runner inventory is invalid or incomplete.', 3)


def github_runners(target, until):
    output = run(['gh', 'api', '--hostname', 'github.com', '--method', 'GET', '--paginate', '--slurp',
                  f'repos/{target.repo}/actions/runners?per_page=100'], until, env=github_environment())
    try:
        pages = strict_json(output)
        if not isinstance(pages, list) or not pages or any(not isinstance(page, dict) for page in pages):
            raise ValueError
        totals = {page.get('total_count') for page in pages}
        if len(totals) != 1:
            raise ValueError
        total = totals.pop()
        if type(total) is not int or total < 0 or any(not isinstance(page.get('runners'), list) for page in pages):
            raise ValueError
        runners = tuple(parse_remote_runner(value) for page in pages for value in page['runners'])
        if len(runners) != total or len({runner.runner_id for runner in runners}) != len(runners):
            raise ValueError
        return runners
    except (AttributeError, TypeError, ValueError, KeyError):
        raise Failure('GitHub runner inventory is invalid or incomplete.', 3)


def registration_token(target, until):
    try:
        output = run(['gh', 'api', '--hostname', 'github.com', '--method', 'POST',
                      f'repos/{target.repo}/actions/runners/registration-token'], until, env=github_environment())
    except Failure as error:
        if error.code == 4:
            raise
        raise Failure('GitHub could not create a runner registration token. Confirm the authenticated account has administrator access to this repository, then retry.', 3) from error
    try:
        value = strict_json(output)
        if not isinstance(value, dict) or value.keys() != {'token', 'expires_at'}:
            raise ValueError
        token, expires = value['token'], value['expires_at']
        if (not isinstance(token, str) or not 20 <= len(token) <= 512 or token.strip() != token or
                any(character.isspace() for character in token) or not isinstance(expires, str)):
            raise ValueError
        expiry = datetime.fromisoformat(expires.replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError
        return token
    except (ValueError, TypeError, AttributeError):
        raise Failure('GitHub returned an invalid or expired registration credential.', 3)


def local_registration(config, target, until):
    script = r'''set -euo pipefail
uid=$1
directory=$2
[[ "$uid" =~ ^[1-9][0-9]*$ ]]
test -d "$directory" && test ! -L "$directory"
test "$(stat -c '%u:%g:%a' "$directory")" = "$uid:$uid:700"
files=(.runner .credentials .credentials_rsaparams)
present=0
for name in "${files[@]}"; do
    if test -e "$directory/$name" || test -L "$directory/$name"; then present=$((present + 1)); fi
done
if test "$present" = 0; then echo Absent; exit; fi
for name in .runner .credentials .credentials_rsaparams; do
    path="$directory/$name"
    test -f "$path" && test ! -L "$path"
    test "$(stat -c '%u:%g' "$path")" = "$uid:$uid"
    test -z "$(find "$path" -prune -perm /022 -print)"
done
for name in .credentials .credentials_rsaparams; do
    test "$(stat -c '%u:%g:%a' "$directory/$name")" = "$uid:$uid:600"
done
echo Present
cat -- "$directory/.runner"
'''
    output = lima(config, ['shell', '--tty=false', config['vm'], '--', 'sudo', '-iu', 'ci',
                           'bash', '-s', '--', str(config['guest_uid']), target.runner_directory],
                  until, input=script)
    if output == 'Absent\n':
        return None
    if not output.startswith('Present\n'):
        raise Failure('Local runner registration files are partial or unsafe. Preserve them and inspect before retrying.', 3)
    try:
        payload = output.removeprefix('Present\n')
        if payload.startswith('\ufeff'):
            payload = payload[1:]
        value = strict_json(payload)
        local = LocalRegistration(value['agentId'], value['agentName'], value['gitHubUrl'], value['workFolder'])
        if (type(local.runner_id) is not int or local.runner_id <= 0 or
                any(not isinstance(field, str) or not field for field in (local.name, local.url, local.work_directory))):
            raise ValueError
        return local
    except (KeyError, TypeError, ValueError):
        raise Failure('Local runner identity metadata is invalid or incomplete.', 3)


def canonical_label_set(labels):
    if not isinstance(labels, tuple) or not labels or any(not isinstance(label, str) or not label or len(label) > 256 or not label.isprintable() for label in labels):
        return None
    canonical = frozenset(label.lower() for label in labels)
    return canonical if len(canonical) == len(labels) else None


def approved_registration_labels(target):
    labels = canonical_label_set(tuple(DEFAULT_REGISTRATION_LABELS) + target.labels)
    if labels is None:
        raise Failure('Configured registration labels are invalid.', 3)
    return labels


def valid_remote(target, runner):
    observed = canonical_label_set(runner.labels)
    return (runner.name == target.name and runner.status == 'offline' and not runner.busy and
            observed == approved_registration_labels(target))


def valid_local(target, local):
    return (local.name == target.name and local.url.rstrip('/').lower() == target.url.lower() and
            local.work_directory == target.work_directory)


def persist_runner_id(config, runner_id):
    if config['version'] not in {2, 3}:
        raise Failure('Automatic runner identity persistence requires a repository profile.', 3)
    home, _, directory, _ = paths()
    path = directory / 'profiles' / (profile_key(config['repo']) + '.json')
    safe_path(path, home)
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
        raise Failure('Repository profile changed before runner identity persistence.', 3)
    try:
        observed = validate_config(strict_json(path.read_text()))
    except (OSError, ValueError) as error:
        raise Failure('Repository profile changed before runner identity persistence.', 3) from error
    expected = dict(config)
    if observed != expected:
        if observed == {**expected, 'runner_id': runner_id}:
            return
        raise Failure('Repository profile changed before runner identity persistence.', 3)
    if 'runner_id' in observed:
        if observed['runner_id'] != runner_id:
            raise Failure('Repository profile records a different runner identity.', 3)
        return
    updated = {**observed, 'runner_id': runner_id}
    validate_config(updated)
    atomic_write(path, (json.dumps(updated, indent=2) + '\n').encode(), 0o600)


def registration_token_script(token):
    if (not isinstance(token, str) or not token or token.strip() != token or
            any(character.isspace() for character in token)):
        raise Failure('Registration credential is not a validated single-line token.', 3)
    delimiter = 'CI_VM_REGISTRATION_TOKEN'
    while delimiter in token:
        delimiter += '_'
    return ("set -euo pipefail\numask 077\nIFS= read -r token <<'" + delimiter + "'\n" + token + "\n" + delimiter +
            '\nexport ACTIONS_RUNNER_INPUT_TOKEN="$token"\nunset token\ncd "$1"\nshift\n'
            'test -x ./config.sh\nexec ./config.sh --unattended "$@"\n')


def configure_runner(config, target, token, until):
    script = registration_token_script(token)
    lima(config, ['shell', '--tty=false', config['vm'], '--', 'sudo', '-iu', 'ci', 'bash', '-s', '--',
                  target.runner_directory, '--url', target.url, '--name', target.name,
                  '--labels', ','.join(target.labels), '--work', target.work_directory], until, input=script)


def enable_registration_unit(config, target, until):
    script = USER_ENV + '''test -f /var/lib/ci-vm/paused
test ! -L /var/lib/ci-vm/paused
ctl enable "$unit"
test "$(ctl is-enabled "$unit")" = enabled
test "$(ctl show "$unit" --property=ActiveState --value)" = inactive
test "$(ctl show "$unit" --property=SubState --value)" = dead
'''
    guest_mutation(config, script, until)


def finish_shared_registration(config, target, until):
    stage = lima(config, ['shell', config['vm'], '--', 'mktemp', '-d', '/tmp/ci-vm-register.XXXXXX'], until).strip()
    if not re.fullmatch(r'/tmp/ci-vm-register\.[A-Za-z0-9]+', stage):
        raise Failure('Cannot establish a fresh shared-registration staging directory.', 3)
    helper = ROOT / 'config/prepare-shared-runner.sh'
    template = ROOT / 'config/ci-vm-runner@.service'
    runtime_probe = ROOT / 'config/container-runtime-state.sh'
    lima(config, ['copy', str(helper), f'{config["vm"]}:{stage}/prepare-shared-runner.sh'], until)
    lima(config, ['copy', str(template), f'{config["vm"]}:{stage}/ci-vm-runner@.service'], until)
    lima(config, ['copy', str(runtime_probe), f'{config["vm"]}:{stage}/container-runtime-state.sh'], until)
    lima(config, ['shell', config['vm'], '--', 'sudo', 'bash', stage + '/prepare-shared-runner.sh',
                  'finish', target.shared_key, '--registration-ready'], until)
    lima(config, ['shell', config['vm'], '--', 'rm', '-rf', '--', stage], until)


def manual_register_runner(config, target, until):
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        raise Failure('Registration must run in your own interactive terminal so the short-lived token is never captured.', 2)
    if (config['guest_user'], config['guest_uid']) != ('ci', 1001):
        raise Failure('Interactive registration requires a supported repository profile and the supplied ci UID 1001 account.', 3)
    if vm_state(config, until) != 'Running':
        raise Failure('Registration requires the selected VM to be Running. No VM was started.', 3)
    ensure_no_package_work(config, until)
    if config['version'] == 3:
        if not shared_setup_gate(config, until):
            raise Failure('The shared member preparation gate is missing. Run the reviewed prepare step before registration.', 3)
    else:
        complete_setup(config, until)
    group_contract(config, until)
    if not group_idle(config, until):
        raise Failure('Registration requires the complete selected VM to remain paused and idle.', 3)
    script = 'set -euo pipefail; cd "$1"; shift; test -x ./config.sh; exec ./config.sh "$@"'
    command = ['limactl', 'shell', config['vm'], '--', 'sudo', '-iu', 'ci', 'bash', '-c', script,
               'ci-vm-register', target.runner_directory, '--url', target.url,
               '--name', target.name, '--labels', ','.join(target.labels), '--work', target.work_directory]
    environment = dict(os.environ, LIMA_HOME=config['lima_home'])
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise Failure('Interactive registration deadline expired before launch. Guest registration state is unconfirmed; the VM remains paused.', 4)
    relay = ProcessSignalRelay()
    result = None
    with relay:
        try:
            result = subprocess.Popen(command, env=environment, start_new_session=True)
            relay.attach(result)
        except OSError as error:
            if relay.received:
                raise Failure('Interactive registration was interrupted before launch completed. Guest registration state is unconfirmed; the VM remains paused.', 4) from error
            raise Failure('Cannot open the interactive registration terminal: ' + str(error)) from error
        try:
            result.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            cleanup_warning = cleanup_process_group(result)
            raise Failure('Interactive registration timed out. Guest registration state is unconfirmed; the VM remains paused.' + cleanup_warning, 4)
        except KeyboardInterrupt:
            cleanup_warning = cleanup_process_group(result)
            raise Failure('Interactive registration was interrupted. Guest registration state is unconfirmed; the VM remains paused.' + cleanup_warning, 4)
    if relay.received:
        cleanup_warning = cleanup_process_group(result)
        raise Failure('Interactive registration was interrupted. Guest registration state is unconfirmed; the VM remains paused.' + cleanup_warning, 4)
    if result.returncode:
        raise Failure('Runner registration did not complete. The VM remains paused; inspect the uncaptured terminal output before retrying.', 3)
    if config['version'] == VERSION:
        print('Legacy manual registration completed. The VM remains paused. Automatic completion is unavailable; follow the legacy verification and service procedure in docs/setup.md.')
    else:
        print('Manual fallback registration completed. The VM remains paused. Rerun register without --manual-token to reconcile the exact runner and finish setup.')


def register_runner(config, until, repo=None, manual_token=False, all_repos=False):
    mutation_scope(config, all_repos)
    if config['version'] not in {VERSION, 2, 3} or (config['guest_user'], config['guest_uid']) != ('ci', 1001):
        raise Failure('Registration requires a supported profile and the supplied ci UID 1001 account.', 3)
    target = registration_target(config, repo)
    if manual_token or config['version'] == VERSION:
        if config['version'] == VERSION and not manual_token:
            raise Failure('Legacy registration remains manual. Repeat with --manual-token from your own terminal.', 2)
        return manual_register_runner(config, target, until)
    github_auth(until)
    if vm_state(config, until) != 'Running':
        raise Failure('Registration requires the selected VM to be Running. No VM was started.', 3)
    ensure_no_package_work(config, until)
    gate = shared_setup_gate(config, until)
    if config['version'] == 3 and not gate and 'runner_id' not in config:
        raise Failure('The shared member preparation gate is missing before registration.', 3)
    if config['version'] != 3 and gate:
        complete_setup(config, until)
    group_contract(config, until)
    if not group_idle(config, until):
        raise Failure('Registration requires the complete selected VM to remain paused and idle.', 3)

    local = local_registration(config, target, until)
    remote = github_runners(target, until)
    matches = tuple(runner for runner in remote if runner.name == target.name)
    profile_id = config.get('runner_id')
    if profile_id is None and local is None and not matches:
        token = registration_token(target, until)
        configure_runner(config, target, token, until)
        token = None
        local = local_registration(config, target, until)
        remote = github_runners(target, until)
        matches = tuple(runner for runner in remote if runner.name == target.name)

    if local is None or not valid_local(target, local) or len(matches) != 1:
        raise Failure('Runner registration state is partial, mismatched, duplicate, or ambiguous. Nothing was replaced or deleted.', 3)
    exact = matches[0]
    if local.runner_id != exact.runner_id or not valid_remote(target, exact):
        raise Failure('Local and GitHub runner identities do not agree exactly. Nothing was replaced or deleted.', 3)
    if profile_id is not None and profile_id != exact.runner_id:
        raise Failure('Repository profile records a different runner identity.', 3)
    persist_runner_id(config, exact.runner_id)

    group_contract(config, until)
    if not group_idle(config, until):
        raise Failure('Runner state changed before service enablement. The VM remains paused.', 3)
    enable_registration_unit(config, target, until)
    enabled_matches = tuple(runner for runner in github_runners(target, until) if runner.name == target.name)
    if (len(enabled_matches) != 1 or enabled_matches[0].runner_id != exact.runner_id or
            not valid_remote(target, enabled_matches[0])):
        raise Failure('GitHub runner identity or dormant state changed after service enablement. The VM remains paused.', 3)
    if config['version'] == 3 and shared_setup_gate(config, until):
        finish_shared_registration(config, target, until)
    group_contract(config, until)
    if not group_idle(config, until):
        raise Failure('Registered unit is not inactive and idle. The VM remains paused.', 3)
    if shared_setup_gate(config, until):
        raise Failure('Registration setup gate remains active. The VM remains paused.', 3)
    print(f'Registered GitHub runner {exact.runner_id} ({exact.name}). Service enabled but inactive; VM remains paused.')

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog='ci-vm', description=__doc__, allow_abbrev=False)
    if argv and argv[0] == '--install':
        parser.add_argument('--install', action='store_true', help=argparse.SUPPRESS)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--adopt', metavar='NAME')
        mode.add_argument('--provision', nargs='?', const='', metavar='NAME', help='create a VM; omit NAME with --repo for a deterministic name')
        mode.add_argument('--share-with', type=repository_value, metavar='OWNER/REPO', help='explicitly reserve a separate runner in an existing paused repository VM')
        parser.add_argument('--yes-create-vm', action='store_true')
        parser.add_argument('--configure-shell', action='store_true', help='append a managed PATH block to Bash and Zsh startup files')
        parser.add_argument('--lima-home')
        parser.add_argument('--guest-user')
        parser.add_argument('--guest-uid', type=int)
        parser.add_argument('--unit')
        parser.add_argument('--repo', type=repository_value)
        parser.add_argument('--runner-id', type=int)
        for flag, key in (('cpus', 'cpus'), ('memory', 'memory_gib'), ('disk', 'disk_gib')):
            low, high = RESOURCE_LIMITS[key]
            unit = 'CPU' if key == 'cpus' else 'GiB'
            parser.add_argument('--' + flag, dest=key, type=int, help=f'new repository VM only; {low}..{high} {unit}, default {RESOURCE_DEFAULTS[key]}; no live resizing')
        parser.add_argument('--timeout', type=timeout_value, default=600)
        args = parser.parse_args(argv)
        with operation_lock():
            install(args)
        return 0
    parser.add_argument('--timeout', type=timeout_value)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--repo', dest='selected_repo', type=repository_value, help='select the exact repository profile')
    selection.add_argument('--legacy', action='store_true', help='select the unchanged legacy configuration')
    commands = parser.add_subparsers(dest='command')
    for name, description in COMMANDS:
        sub = commands.add_parser(name, help=description, description=description, allow_abbrev=False)
        sub.add_argument('--timeout', type=timeout_value, default=argparse.SUPPRESS)
        selection = sub.add_mutually_exclusive_group()
        selection.add_argument('--repo', dest='selected_repo', type=repository_value, default=argparse.SUPPRESS)
        selection.add_argument('--legacy', action='store_true', default=argparse.SUPPRESS)
        if name == 'setup':
            sub.add_argument('repo', type=repository_value, metavar='OWNER/REPO', help='repository name or https://github.com/OWNER/REPO URL')
        if name == 'register':
            sub.add_argument('repo', nargs='?', type=repository_value, metavar='OWNER/REPO', help='required only for an unassigned legacy profile')
            sub.add_argument('--manual-token', action='store_true', help='troubleshooting fallback; open an uncaptured token prompt')
            sub.add_argument('--all-repos', action='store_true', help='acknowledge effects on every repository sharing the selected VM')
        if name == 'logs':
            sub.add_argument('--lines', type=int, choices=range(1, 1001), default=100, metavar='1..1000')
        if name in {'pause', 'resume', 'restart', 'packages'}:
            sub.add_argument('--all-repos', action='store_true', help='acknowledge effects on every repository sharing the selected VM')
        if name == 'packages':
            sub.add_argument('packages', nargs='+', type=package_argument, metavar='PACKAGE[=VERSION]')
            sub.add_argument('--apply', action='store_true', help='install only after a confirmed preview and repeated paused-idle checks')
            sub.add_argument('--yes', action='store_true', help='with --apply, skip confirmation for this exact package request only')
            sub.add_argument('--json', action='store_true', help='print an allowlisted receipt without raw APT output')
        if name == 'verify-run':
            sub.add_argument('run_id', type=positive_id, metavar='RUN_ID')
            sub.add_argument('--expect-sha', required=True, metavar='FULL_SHA')
            sub.add_argument('--expect-event', required=True, metavar='EVENT')
            sub.add_argument('--expect-runner-id', required=True, type=positive_id, metavar='ID')
            sub.add_argument('--job', dest='jobs', action='append', required=True, metavar='EXACT_JOB_NAME')
            sub.add_argument('--json', action='store_true')
    if sum(arg == '--repo' or arg.startswith('--repo=') for arg in argv) > 1 or argv.count('--legacy') > 1:
        parser.error('supply each repository or legacy selector only once')
    args = parser.parse_args(argv)
    if args.timeout is None:
        args.timeout = 600 if args.command in {'packages', 'register'} else 30
    if args.legacy and args.selected_repo:
        parser.error('choose --repo or --legacy, not both')
    if args.command is None:
        overview()
        return 0
    if args.command == 'packages':
        if not args.apply:
            return packages(args)
        try:
            with operation_lock():
                return packages(args)
        except Failure as error:
            print_receipt({'operation': 'packages', 'outcome': 'failed', 'error': str(error), 'paused': 'unverified'}, args.json)
            return error.code
    if args.command == 'verify-run':
        return verify_run(args)
    if args.command == 'profiles':
        if args.legacy or args.selected_repo:
            parser.error('profiles lists all local configurations; omit --repo and --legacy')
        configs = configurations()
        sections = []
        for _, config in configs:
            resources = config.get('resources')
            lines = [f'VM {config["vm"]}', f'Lima home {config["lima_home"]}']
            if resources:
                lines.append(f'Creation request {resources["cpus"]} CPU / {resources["memory_gib"]} GiB memory / {resources["disk_gib"]} GiB disk')
            else:
                lines.append('Creation resources not recorded.')
            if config['version'] == 3:
                lines.append('Shares VM with anchor ' + config['shared_with'])
                lines.append('Separate runner service ' + config['unit'])
            sections.append((config.get('repo', 'Legacy configuration') + (' [--legacy]' if config['version'] == VERSION else ''), lines))
        print_sections('Local profiles', 'No VM or GitHub queries were made.', sections or [('No configurations', ('Install an approved repository VM first.',))])
        return 0
    if args.command == 'setup':
        if args.selected_repo and args.selected_repo != args.repo:
            parser.error('setup repository differs from --repo')
        setup_guide(load_config(None if args.legacy else args.repo, args.legacy), args.repo)
        return 0
    if args.command == 'register':
        with operation_lock():
            register_runner(load_config(args.selected_repo, args.legacy), deadline(args.timeout), args.repo, args.manual_token, args.all_repos)
        return 0
    until = deadline(args.timeout)
    if args.command in {'pause', 'resume', 'restart'}:
        with operation_lock():
            maintain(load_config(args.selected_repo, args.legacy), args.command, until, all_repos=args.all_repos)
    else:
        report(load_config(args.selected_repo, args.legacy), args.command, until, getattr(args, 'lines', 100))
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
