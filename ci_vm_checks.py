import re
from typing import NamedTuple


class PackageRequest(NamedTuple):
    name: str
    version: str | None = None

    def argument(self):
        return self.name + ('=' + self.version if self.version else '')


PROTECTED_PACKAGES = {
    'docker-ce', 'docker-ce-cli', 'docker-ce-rootless-extras', 'containerd.io',
    'docker-buildx-plugin', 'docker-compose-plugin',
}
PACKAGE = re.compile(r'[a-z0-9][a-z0-9+.-]{0,79}\Z')
VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,127}\Z')


def package_request(value):
    name, separator, version = value.partition('=')
    if not PACKAGE.fullmatch(name) or name.endswith(('-', '+')) or separator and not VERSION.fullmatch(version):
        raise ValueError('Use an exact Debian package name or NAME=VERSION; options, paths, URLs, patterns, and removal suffixes are refused.')
    return PackageRequest(name, version if separator else None)


def package_observation(output, requests):
    before, separator, simulation = output.partition('\nPLAN\n')
    if not separator:
        raise ValueError('Package probe did not return a complete observation.')
    installed = {}
    holds = set()
    reboot = None
    for line in before.splitlines():
        fields = line.split('\t')
        if len(fields) == 3 and fields[0] == 'P':
            name, version = fields[1:]
            if name in installed or not PACKAGE.fullmatch(name) or version != '-' and not VERSION.fullmatch(version):
                raise ValueError('Installed package identity or version is inconclusive.')
            installed[name] = None if version == '-' else version
        elif len(fields) == 2 and fields[0] == 'H':
            name = fields[1].removesuffix(':arm64')
            if not PACKAGE.fullmatch(name):
                raise ValueError('Held package identity is inconclusive.')
            holds.add(name)
        elif len(fields) == 2 and fields[0] == 'R' and fields[1] in {'yes', 'no'} and reboot is None:
            reboot = fields[1] == 'yes'
        else:
            raise ValueError('Package observation contains an unknown field.')
    if set(installed) != {request.name for request in requests} or reboot is None:
        raise ValueError('Package observation is missing requested identities.')
    changes = {}
    configured = set()
    summary = None
    for line in simulation.splitlines():
        count = re.fullmatch(r'(\d+) upgraded, (\d+) newly installed, (\d+) to remove and (\d+) not upgraded\.', line)
        if count:
            if summary is not None or int(count[3]) != 0:
                raise ValueError('APT transaction summary is duplicated or includes removals.')
            summary = int(count[1]) + int(count[2])
        if line.startswith('Remv '):
            raise ValueError('Package removals are refused.')
        if line.startswith('Inst '):
            match = re.fullmatch(r'Inst (\S+)(?: \[([^\]]+)\])? \((\S+)(?: .*?)?\)', line)
            if not match:
                raise ValueError('APT installation transaction is inconclusive.')
            name, previous, version = match.groups()
            name = name.removesuffix(':arm64')
            if not PACKAGE.fullmatch(name) or not VERSION.fullmatch(version) or previous and not VERSION.fullmatch(previous) or name in changes:
                raise ValueError('APT installation identity or version is inconclusive.')
            if name in holds | PROTECTED_PACKAGES:
                raise ValueError('A held or protected base Docker package would change. Review separate maintenance.')
            changes[name] = {'name': name, 'previous': previous, 'version': version}
            if len(changes) > 256:
                raise ValueError('Package transaction exceeds 256 changes. Review separate maintenance.')
        elif line.startswith('Conf '):
            match = re.fullmatch(r'Conf (\S+) \((\S+)(?: .*?)?\)', line)
            if not match:
                raise ValueError('APT configuration transaction is inconclusive.')
            name, version = match.groups()
            name = name.removesuffix(':arm64')
            if name in configured or name not in changes or changes[name]['version'] != version:
                raise ValueError('APT would configure an unplanned or incomplete package.')
            configured.add(name)
        elif re.match(r'^(E:|W:|Err:|Inst\b|Conf\b|Remv\b)', line):
            raise ValueError('APT reported an error, warning, or unknown transaction. Review package indexes and configuration separately.')
    if summary is None or summary != len(changes) or configured != changes.keys():
        raise ValueError('APT transaction is missing package configuration steps.')
    intended = {}
    for request in requests:
        version = changes[request.name]['version'] if request.name in changes else installed[request.name]
        if version is None or request.version is not None and version != request.version:
            raise ValueError('APT did not resolve the exact requested package version.')
        intended[request.name] = version
    for name, change in changes.items():
        intended[name] = change['version']
    return {'installed': installed, 'changes': sorted(changes.values(), key=lambda change: change['name']),
            'intended': intended, 'held': sorted(holds), 'reboot_required': reboot}


def assess_run(run, jobs, expected):
    required = {'id', 'repository', 'head_sha', 'event', 'run_attempt', 'status', 'conclusion'}
    if not isinstance(run, dict) or not required <= run.keys():
        raise ValueError('GitHub run evidence is incomplete.')
    repository = run['repository']
    if not isinstance(repository, dict) or not isinstance(repository.get('full_name'), str):
        raise ValueError('GitHub repository identity is missing.')
    if type(run['id']) is not int or run['id'] != expected['run_id'] or repository['full_name'].lower() != expected['repo']:
        raise ValueError('GitHub run or repository identity differs.')
    if run['head_sha'] != expected['sha'] or run['event'] != expected['event']:
        raise ValueError('GitHub run commit or event differs.')
    if type(run['run_attempt']) is not int or run['run_attempt'] < 1:
        raise ValueError('GitHub run attempt is invalid.')
    if not isinstance(run['status'], str) or run['status'] not in {'queued', 'in_progress', 'completed', 'waiting', 'requested', 'pending'}:
        raise ValueError('GitHub run status is unknown.')
    if run['conclusion'] is not None and not isinstance(run['conclusion'], str) or run['conclusion'] not in {None, 'success', 'failure', 'neutral', 'cancelled', 'skipped', 'timed_out', 'action_required', 'stale', 'startup_failure'}:
        raise ValueError('GitHub run conclusion is unknown.')
    if not isinstance(jobs, list):
        raise ValueError('GitHub jobs evidence is invalid.')
    names = {}
    ids = set()
    for job in jobs:
        if not isinstance(job, dict) or type(job.get('id')) is not int or job['id'] <= 0 or job['id'] in ids:
            raise ValueError('GitHub job identities are missing or duplicated.')
        if not isinstance(job.get('name'), str) or not job['name'].strip() or job['name'] in names:
            raise ValueError('GitHub job names are missing or duplicated.')
        if type(job.get('run_id')) is not int or job['run_id'] != run['id'] or job.get('head_sha') != run['head_sha'] or type(job.get('run_attempt')) is not int or job['run_attempt'] != run['run_attempt']:
            raise ValueError('GitHub job belongs to another run or commit.')
        ids.add(job['id'])
        names[job['name']] = job
    selected = []
    issues = []
    for name, job in names.items():
        runner_id = job.get('runner_id')
        if runner_id is not None and (type(runner_id) is not int or runner_id <= 0):
            raise ValueError('GitHub job runner identity is invalid.')
        if runner_id == expected['runner_id'] and name not in expected['jobs']:
            issues.append(f'Unexpected job ran on intended runner: {name}')
    for name in expected['jobs']:
        job = names.get(name)
        if job is None:
            issues.append(f'Missing expected job: {name}')
            continue
        labels = job.get('labels')
        runner_name = job.get('runner_name')
        if not isinstance(labels, list) or len(labels) > 100 or any(not isinstance(label, str) or not label or len(label) > 256 or not label.isprintable() for label in labels):
            raise ValueError('GitHub runner labels are inconclusive.')
        if not isinstance(job.get('status'), str) or job['status'] not in {'queued', 'in_progress', 'completed', 'waiting', 'pending'}:
            raise ValueError('GitHub job status is inconclusive.')
        if job.get('conclusion') is not None and not isinstance(job['conclusion'], str):
            raise ValueError('GitHub job conclusion is inconclusive.')
        if runner_name is not None and (not isinstance(runner_name, str) or len(runner_name) > 256 or not runner_name.isprintable()):
            raise ValueError('GitHub runner name is inconclusive.')
        if job.get('status') != 'completed' or job.get('conclusion') != 'success':
            issues.append(f'Expected job has not completed successfully: {name}')
        if type(job.get('runner_id')) is not int or job['runner_id'] != expected['runner_id'] or not isinstance(runner_name, str) or not runner_name.strip():
            issues.append(f'Expected runner identity differs: {name}')
        if not {'self-hosted', 'linux', 'arm64'} <= {label.lower() for label in labels}:
            issues.append(f'Expected runner labels differ: {name}')
        selected.append({'name': name, 'status': job.get('status'), 'conclusion': job.get('conclusion'),
                         'runner_id': job.get('runner_id'), 'runner_name': runner_name, 'labels': labels})
    if run['status'] != 'completed' or run['conclusion'] != 'success':
        issues.append('The workflow run has not completed successfully.')
    return {'verified': not issues, 'repo': expected['repo'], 'run_id': run['id'], 'attempt': run['run_attempt'],
            'sha': run['head_sha'], 'event': run['event'], 'status': run['status'], 'conclusion': run['conclusion'],
            'jobs': selected, 'issues': issues}
