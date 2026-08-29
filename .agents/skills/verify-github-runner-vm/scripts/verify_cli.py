#!/usr/bin/env python3
import argparse
import ast
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[4]
MARKER = '.ci-vm-verification.json'
GROUPS = {
    'onboarding': [
        'InstallationTests.test_install_rerun_shells_and_stopped_readonly',
        'InstallationTests.test_shell_configuration_is_opt_in_and_idempotent',
        'InstallationTests.test_install_from_extracted_archive_without_git_or_retained_source',
        'InstallationTests.test_documented_pipeline_fetch_failure_and_success_in_both_shells',
        'CommandTests.test_setup_preserves_adopted_services_and_rejects_another_repository',
        'CommandTests.test_terminal_color_respects_no_color_and_dumb_terminal',
        'CommandTests.test_profile_names_are_bounded_and_distinguish_similar_repositories',
        'CommandTests.test_provisioning_refuses_oversized_socket_path_before_profile_write',
        'CommandTests.test_register_opens_only_the_interactive_token_prompt',
        'CommandTests.test_manual_registration_uses_inherited_terminal_and_deadline',
        'CommandTests.test_register_refuses_capture_wrong_state_and_missing_shared_gate',
        'CommandTests.test_register_command_routes_selected_repository',
        'CommandTests.test_unattended_registration_transports_token_only_through_stdin',
        'CommandTests.test_registration_token_heredoc_rejects_delimiter_collision_and_multiline_values',
        'CommandTests.test_unattended_register_create_recover_noop_and_refuse',
        'CommandTests.test_registration_persists_only_exact_runner_id_with_compare_and_swap',
        'CommandTests.test_registration_enables_inactive_unit_and_finishes_shared_gate',
        'CommandTests.test_enable_registration_unit_never_starts_listener',
        'CommandTests.test_registration_auth_token_and_pagination_boundaries_fail_closed',
        'CommandTests.test_local_registration_executes_present_file_boundary_and_refuses_readable_credentials',
        'CommandTests.test_local_registration_treats_only_env_and_path_as_unregistered',
        'CommandTests.test_local_registration_accepts_one_leading_bom_and_refuses_other_bom_positions',
        'CommandTests.test_registration_refuses_online_runner_and_post_enable_drift',
        'CommandTests.test_register_default_timeout_is_ten_minutes_and_explicit_value_wins',
        'CommandTests.test_finish_shared_registration_stages_both_files_and_cleans_only_after_success',
        'CommandTests.test_repository_setup_guide_reports_inactive_enable_then_doctor_resume_status',
    ],
    'profiles': [
        'InstallationTests.test_repository_profiles_route_only_the_selected_vm_and_preserve_legacy',
        'InstallationTests.test_duplicate_physical_vm_binding_refused_through_lima_home_alias',
        'InstallationTests.test_profile_filename_permissions_symlinks_and_duplicate_keys_refused',
        'CommandTests.test_repeated_or_conflicting_selectors_refuse_before_reading_state',
        'CommandTests.test_abbreviated_selectors_refuse_before_reading_or_changing_state',
    ],
    'sharing': [
        'InstallationTests.test_explicit_shared_attachment_rerun_and_cross_owner_selection',
        'InstallationTests.test_shared_attachment_requires_existing_idle_anchor_and_valid_reference',
        'CommandTests.test_shared_registration_requires_all_repositories_scope',
        'CommandTests.test_shared_inventory_and_secondary_cgroups_refuse_mutation',
        'CommandTests.test_shared_resume_failure_restores_pause_and_setup_gate_blocks',
        'test_provision.SharedPreparationTests',
    ],
    'maintenance': [
        'InstallationTests.test_full_pause_and_pending_with_fake_limactl',
        'InstallationTests.test_fake_github_exact_readback_and_mismatch',
        'CommandTests.test_each_restart_guard_is_required',
        'CommandTests.test_stopped_resume_never_starts_vm',
        'CommandTests.test_restart_preserves_pause',
        'CommandTests.test_failure_reports_only_allowlisted_diagnostics',
    ],
    'packages': [
        'CommandTests.test_package_requests_and_simulation_are_exact_and_bounded',
        'CommandTests.test_package_preview_and_confirmation_never_mutate',
        'CommandTests.test_package_apply_verifies_versions_then_clears_gate_and_leaves_paused',
        'CommandTests.test_package_busy_contract_and_transaction_drift_refuse',
        'CommandTests.test_package_failures_keep_gate_and_block_resume_restart',
    ],
    'actions': [
        'CommandTests.test_verify_run_checks_attempt_pages_and_allows_other_hosted_jobs',
        'CommandTests.test_verify_run_refuses_missing_duplicate_pending_failed_or_wrong_jobs',
        'CommandTests.test_verify_run_rejects_incomplete_pages_and_concurrent_rerun',
        'CommandTests.test_verify_run_rejects_conflicting_profile_id_and_invalid_expectations_without_gh',
        'CommandTests.test_verify_run_refuses_unlisted_job_on_selected_runner_and_invalid_runner_id',
    ],
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def owned(run):
    require(not run.is_symlink() and run.is_dir(), 'Run must be an owned directory, not a symlink.')
    marker = run / MARKER
    require(not marker.is_symlink() and marker.is_file(), 'Missing verification ownership marker.')
    data = json.loads(marker.read_text())
    require(data == {'kind': 'ci-vm-verification-v1', 'path': str(run.resolve()), 'uid': os.getuid()},
            'Ownership marker does not match this run.')
    require(run.stat().st_uid == os.getuid() and stat.S_IMODE(run.stat().st_mode) == 0o700,
            'Run must be owned by this user with mode 700.')
    for name in ('scratch', 'evidence'):
        require(not (run / name).is_symlink(), f'Refusing symlink: {name}')
    return run


def environment(run):
    scratch = run / 'scratch'
    require(scratch.is_dir(), 'Scratch was cleaned; launch a fresh run.')
    return {
        'HOME': str(scratch / 'isolated home'),
        'PATH': str(scratch / 'tools') + os.pathsep + '/usr/bin:/bin',
        'TMPDIR': str(scratch / 'tmp'),
        'ZDOTDIR': str(scratch / 'isolated home'),
        'TERM': 'xterm-256color', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': str(scratch / 'source/tests'),
        'VERIFY_CALLS': str(run / 'evidence/boundary.jsonl'),
        'VERIFY_SHARED_STATE': str(run / 'scratch/shared-state.json'),
    }


def execute(run, label, argv, expected=0, contains=(), timeout=30):
    env = environment(run)
    out = bytearray()
    master, slave = pty.openpty()
    process = None
    timed_out = False
    try:
        process = subprocess.Popen(argv, cwd=run / 'scratch/source', env=env,
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        slave = None
        until = time.monotonic() + timeout
        while True:
            if time.monotonic() >= until:
                timed_out = True
                break
            if select.select([master], [], [], min(0.1, max(0, until - time.monotonic())))[0]:
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                out.extend(data)
        if not timed_out:
            process.wait(timeout=max(0.1, until - time.monotonic()))
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        os.close(master)
        if slave is not None:
            os.close(slave)
        evidence = run / 'evidence'
        (evidence / (label + '.terminal.txt')).write_bytes(out)
        save(evidence / (label + '.json'), {
            'argv': argv, 'exit_code': process.returncode if process else None,
            'expected_exit': expected, 'timed_out': timed_out, 'surface': 'PTY',
            'HOME': env['HOME'], 'boundary': 'fake Lima/GitHub; no live proof',
        })
    rendered = out.decode(errors='replace').replace('\r\n', '\n')
    require(not timed_out, f'{label}: command exceeded {timeout}s')
    require(process.returncode == expected, f'{label}: unexpected exit {process.returncode}; see evidence')
    for value in contains:
        require(value in rendered, f'{label}: missing expected text {value!r}')
    return rendered


def snapshot(home):
    result = {}
    for path in sorted(home.rglob('*')):
        require(not path.is_symlink(), 'Unexpected symlink in isolated HOME.')
        if path.is_file():
            info = path.stat()
            result[str(path.relative_to(home))] = {
                'sha256': digest(path), 'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns,
            }
    return result


def calls(run):
    return [json.loads(line) for line in (run / 'evidence/boundary.jsonl').read_text().splitlines()]


def launch():
    require(sys.version_info >= (3, 10), 'Python 3.10 or newer is required.')
    require(os.getuid() != 0, 'Run as a normal user, not root.')
    run = Path(tempfile.mkdtemp(prefix='ci-vm-proof-')).resolve()
    save(run / MARKER, {'kind': 'ci-vm-verification-v1', 'path': str(run), 'uid': os.getuid()})
    (run / 'evidence').mkdir()
    print(run, flush=True)
    try:
        for name in ('isolated home', 'tools', 'tmp', 'source'):
            (run / 'scratch' / name).mkdir(parents=True)
        source = run / 'scratch/source'
        module = ast.parse((ROOT / 'ci_vm.py').read_text())
        install_files = next(ast.literal_eval(node.value) for node in module.body
                             if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'INSTALL_FILES' for t in node.targets))
        files = ['ci_vm.py', 'install.sh', 'bootstrap.sh', *install_files,
                 'tests/test_ci_vm.py', 'tests/test_provision.py']
        hashes = {}
        for name in files:
            require(not Path(name).is_absolute() and '..' not in Path(name).parts, 'Unsafe source path.')
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
            hashes[name] = digest(target)
        save(run / 'evidence/source.json', {'files': hashes, 'helper_sha256': digest(Path(__file__)),
             'python': sys.version, 'scope': 'Snapshot of working bytes, including uncommitted changes.'})
        tool_dir = run / 'scratch/tools'
        (tool_dir / 'python3').symlink_to(sys.executable)
        stub = '#!' + sys.executable + '\n' + '''import hashlib, json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['VERIFY_CALLS'], 'a') as log:
    log.write(json.dumps({'tool': name, 'argv': args, 'lima_home': os.environ.get('LIMA_HOME')}) + '\\n')
state_file = pathlib.Path(os.environ['VERIFY_SHARED_STATE'])
state = json.loads(state_file.read_text()) if state_file.exists() else {}
if name == 'limactl' and args == ['list', '--json']:
    print(json.dumps([{'name': n, 'status': 'Running' if n == 'one' and state else 'Stopped'} for n in ('one', 'two', 'legacy')]))
elif name == 'limactl' and args[:2] == ['shell', 'one'] and state:
    script = sys.stdin.read()
    unit = args[8]
    if 'sort -u' in script:
        print('\\n'.join(state['units']))
    elif 'SharedSetup=' in script:
        print('SharedSetup=clear')
    elif 'echo PackageGate=' in script:
        print('PackageGate=clear')
    elif 'UnitHash' in script:
        directory = pathlib.Path.home() / '.local/share/github-runner-vm/config'
        content = (directory / ('ci-vm-runner@.service' if '@' in unit else 'ci-vm-runner.service')).read_bytes()
        content = content.replace(b'@KEY@', unit.removeprefix('ci-vm-runner@').removesuffix('.service').encode())
        print('LoadState=loaded\\nFragmentPath=/etc/systemd/user/' + unit + '\\nDropInPaths=\\nNeedDaemonReload=no\\nTransient=no')
        print('UnitHash=' + hashlib.sha256(content).hexdigest())
        print('UnitOwner=0:644\\nActualUID=1001\\nMarkerOwner=0:755')
    elif 'CgroupEmpty' in script:
        print('ActiveState=inactive\\nSubState=dead\\nMainPID=0\\nControlPID=0\\nControlGroup=\\nJob=0\\nPaused=yes\\nRunners=0\\nContainers=0\\nRuntimeDrift=no\\nJobs=0\\nCgroupEmpty=yes')
    elif 'ln -- "$temporary" "$marker"' in script:
        pass
    else:
        sys.exit(97)
else:
    sys.exit(97)
'''
        for name in ('limactl', 'gh', 'curl'):
            tool = tool_dir / name
            tool.write_text(stub)
            tool.chmod(0o755)
        save(run / 'evidence/tools.json', {name: digest(tool_dir / name) for name in ('limactl', 'gh', 'curl')})
        (run / 'evidence/boundary.jsonl').touch()
        for vm, repo in (('legacy', None), ('one', 'verify/one'), ('two', 'verify/two')):
            args = ['/bin/bash', str(source / 'install.sh'), '--adopt', vm]
            if repo:
                args += ['--repo', repo]
            execute(run, 'launch-' + vm, args, contains=('Installed ci-vm.',))
        save(run / 'evidence/launch.json', {'ready': True, 'profiles': ['legacy', 'verify/one', 'verify/two']})
        return run
    except BaseException:
        cleanup(run)
        raise


def doctor(run):
    owned(run)
    env = environment(run)
    home = Path(env['HOME'])
    share = home / '.local/share/github-runner-vm'
    hashes = json.loads((run / 'evidence/source.json').read_text())['files']
    for name, expected in hashes.items():
        require(digest(run / 'scratch/source' / name) == expected, 'Source snapshot changed: ' + name)
        if name not in {'install.sh', 'bootstrap.sh'} and not name.startswith('tests/'):
            require(digest(share / name) == expected, 'Installed source differs: ' + name)
    for name in ('limactl', 'gh', 'curl'):
        require(shutil.which(name, path=env['PATH']) == str(run / 'scratch/tools' / name), 'Boundary shadowing failed.')
        require(digest(run / 'scratch/tools' / name) == json.loads((run / 'evidence/tools.json').read_text())[name], 'Boundary stub changed.')
    before = snapshot(home)
    before_calls = calls(run)
    execute(run, 'doctor-help', [str(home / '.local/bin/ci-vm'), '--help'], contains=('verify-run', 'profiles'))
    require(snapshot(home) == before and calls(run) == before_calls, 'Help changed state or called an external tool.')
    save(run / 'evidence/doctor.json', {'ready': True, 'installed_source_matches': True,
         'help_read_only': True, 'live_vm_health': 'not checked'})


def drive(run):
    doctor(run)
    home = Path(environment(run)['HOME'])
    cli = str(home / '.local/bin/ci-vm')
    before = snapshot(home)
    before_calls = calls(run)
    save(run / 'evidence/home-before.json', before)
    local = [
        ('overview', [], 0, ('ci-vm', 'Connect a repository')),
        ('profiles', ['profiles'], 0, ('verify/one', 'verify/two', 'legacy')),
        ('setup', ['setup', 'verify/one'], 0, ('VM one', 'Read-only guide.', 'setup is not verified')),
        ('setup-selected', ['--repo', 'verify/two', 'setup', 'verify/two'], 0, ('VM two',)),
        ('setup-legacy', ['--legacy', 'setup', 'verify/legacy'], 0, ('VM legacy',)),
        ('ambiguous', ['status'], 2, ()),
        ('unknown', ['status', '--repo', 'verify/missing'], 2, ()),
        ('duplicate-selector', ['--repo', 'verify/one', 'status', '--repo', 'verify/two'], 2, ()),
        ('abbreviated-selector', ['--rep', 'verify/one', 'status'], 2, ()),
    ]
    for name, args, code, output in local:
        execute(run, 'drive-' + name, [cli, *args], code, output)
    require(calls(run) == before_calls, 'A local-only path called an external tool.')
    for name, args in (
        ('one', ['--repo', 'verify/one', 'status']),
        ('two', ['status', '--repo', 'verify/two']),
        ('legacy', ['status', '--legacy']),
    ):
        execute(run, 'drive-status-' + name, [cli, *args], contains=(f'VM {name}: Stopped',))
    added = calls(run)[len(before_calls):]
    require(len(added) == 3, 'Expected exactly three read-only VM queries.')
    require(all(c['tool'] == 'limactl' and c['argv'] == ['list', '--json']
                and Path(c['lima_home']).is_relative_to(home) for c in calls(run)),
            'Unexpected external call or Lima home.')
    after = snapshot(home)
    save(run / 'evidence/home-after.json', after)
    require(before == after, 'Read-only drive changed installed files, profiles, or shell files.')
    profiles = [json.loads(p.read_text()) for p in (home / '.config/github-runner-vm/profiles').glob('*.json')]
    require({p['repo']: p['vm'] for p in profiles} == {'verify/one': 'one', 'verify/two': 'two'}, 'Wrong profile bindings.')
    require(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (home / '.config/github-runner-vm/profiles').glob('*.json')), 'Unsafe profile permissions.')
    save(run / 'evidence/drive.json', {'feature': 'profiles', 'passed': True,
         'entries': [entry[0] for entry in local] + ['status-one', 'status-two', 'status-legacy'],
         'home_unchanged': True, 'only_read_only_boundary_calls': True,
         'limitations': ['No live VM, GitHub request, registration, package installation, or workflow run.']})


def checks(run, feature):
    doctor(run)
    execute(run, 'checks-' + feature, [sys.executable, '-m', 'unittest', '-v',
            *[name if name.startswith('test_provision.') else 'test_ci_vm.' + name for name in GROUPS[feature]]], timeout=120)


def drive_sharing(run):
    doctor(run)
    home = Path(environment(run)['HOME'])
    cli = str(home / '.local/bin/ci-vm')
    source = run / 'scratch/source'
    state = run / 'scratch/shared-state.json'
    save(state, {'units': ['ci-vm-runner.service']})
    execute(run, 'sharing-no-implicit-reuse', ['/bin/bash', str(source / 'install.sh'), '--adopt', 'one', '--repo', 'another/three'], 2, ('already bound',))
    command = ['/bin/bash', str(source / 'install.sh'), '--repo', 'another/three', '--share-with', 'verify/one']
    execute(run, 'sharing-attach', command, contains=('Shared profile reserved', 'verify/one', 'another/three'))
    before = snapshot(home)
    before_calls = calls(run)
    execute(run, 'sharing-rerun', command, contains=('Shared profile reserved',))
    after = snapshot(home)
    require({name: value for name, value in after.items() if name.startswith('.config/')} ==
            {name: value for name, value in before.items() if name.startswith('.config/')} and calls(run) == before_calls,
            'Exact sharing rerun changed reservation or queried a guest.')
    require({name: value['sha256'] for name, value in before.items()} == {name: value['sha256'] for name, value in after.items()}, 'Reinstallation changed installed bytes.')
    before = after
    profiles = [json.loads(p.read_text()) for p in (home / '.config/github-runner-vm/profiles').glob('*.json')]
    child = next(p for p in profiles if p['repo'] == 'another/three')
    require(child['version'] == 3 and child['shared_with'] == 'verify/one' and child['vm'] == 'one', 'Wrong shared identity.')
    save(state, {'units': ['ci-vm-runner.service', child['unit']]})
    execute(run, 'sharing-profiles', [cli, 'profiles'], contains=('another/three', 'Shares VM with anchor verify/one'))
    execute(run, 'sharing-guide', [cli, 'setup', 'another/three'], contains=('resume --all-repos', 'RUNNER_KEY=', child['unit']))
    execute(run, 'sharing-scope-refusal', [cli, '--repo', 'another/three', 'pause'], 2, ('--all-repos', 'verify/one', 'another/three'))
    execute(run, 'sharing-pause', [cli, '--repo', 'another/three', 'pause', '--all-repos'], contains=('Affected repositories:', 'verify/one', 'another/three', 'Paused.'))
    require(snapshot(home) == before, 'Sharing drive changed installed source or profiles.')
    require(all(c['tool'] == 'limactl' and c['argv'][0] in {'list', 'shell'} for c in calls(run)), 'Unexpected boundary tool.')
    save(run / 'evidence/drive-sharing.json', {'feature': 'sharing', 'passed': True,
         'shared_repository': 'another/three', 'anchor': 'verify/one', 'guest': 'synthetic Running and paused/idle',
         'implicit_reuse_refused': True, 'exact_rerun_unchanged': True, 'all_repos_required': True,
         'limitations': ['Fake Lima boundary only. No live guest preparation, package change, registration, or GitHub job.']})


def cleanup(run):
    owned(run)
    evidence = run / 'evidence'
    before = {str(p.relative_to(evidence)): digest(p) for p in evidence.rglob('*')
              if p.is_file() and p.name != 'cleanup.json'}
    scratch = run / 'scratch'
    if scratch.exists():
        shutil.rmtree(scratch)
    require(all(digest(evidence / name) == value for name, value in before.items()), 'Evidence changed during cleanup.')
    save(evidence / 'cleanup.json', {'scratch_removed': not scratch.exists(), 'retained_sha256': before})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('run', 'launch', 'doctor', 'drive', 'checks', 'cleanup'))
    parser.add_argument('run_dir', nargs='?', type=Path)
    parser.add_argument('--feature', choices=tuple(GROUPS), default='profiles')
    args = parser.parse_args()
    if args.phase in {'run', 'launch'}:
        require(args.run_dir is None, 'Launch allocates its own directory; do not supply one.')
        run = launch()
    else:
        require(args.run_dir is not None, 'Supply the directory printed by launch.')
        run = owned(args.run_dir.absolute())
    try:
        if args.phase in {'doctor', 'run'}:
            doctor(run)
        if args.phase in {'drive', 'run'}:
            require(args.feature in {'profiles', 'sharing'}, 'PTY drive covers profiles and sharing; use checks for other features.')
            (drive_sharing if args.feature == 'sharing' else drive)(run)
        if args.phase == 'checks':
            checks(run, args.feature)
        if args.phase in {'cleanup', 'run'}:
            cleanup(run)
    except BaseException as error:
        save(run / 'evidence/failure.json', {'phase': args.phase, 'error': str(error)})
        cleanup(run)
        raise


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(f'verification: {error}', file=sys.stderr)
        sys.exit(1)
