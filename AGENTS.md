# Agent instructions

This project helps a human operate one GitHub Actions runner in a Lima Linux VM on an Apple Silicon Mac.
GitHub owns workflow scheduling and checks. Do not add a workflow emulator, host daemon, dashboard, fleet manager, or plugin framework.

Read `docs/llm-setup.md` before setup or maintenance work.
Inspect the exact VM, Lima home, CI account, service, target repository, and runner ID before proposing changes.
An adopted service is observational unless it matches the maintenance contract.

## Authority

Local source edits and temporary-home tests do not authorize changing a VM, runner registration, workflows in another repository, power settings, or existing data.
Ask before provisioning, service migration, disruptive tests, dispatching workflows, registration, deletion, or external writes.
Before publication, show the exact destination and sanitized file list for confirmation.
The owner selected MIT. Preserve `LICENSE` and its copyright notice.

Never copy Mac credentials into the guest.
Never inspect or publish `.credentials*`, `.runner*`, VM disks, tokens, personal paths, private repository details, or raw logs.
Registration uses only a fresh short-lived token in the human's terminal.

## Maintenance invariants

Do not signal active listeners or workers. GitHub `busy: false` does not close the assignment race.
Pause requests persist until resume. Timeout leaves the VM running.
VM restart requires a confirmed paused service, no pending service job, no remaining runner processes, and no running containers.
Unknown or incomplete evidence refuses mutation. Do not add a force flag or silently recreate a VM.
The root-owned unit is an operational contract, not protection against a compromised CI user.

Preserve workflow events, filters, permissions, job dependencies, checks, concurrency, cancellation, artifacts, and cleanup when proposing migrations.
Keep Xcode and macOS jobs on macOS. Public fork jobs must not automatically use a persistent runner.
This repository's `.github/workflows/` must use GitHub-hosted runners. The self-hosted example stays in `examples/`.

## Verification

Run `python3 -m unittest discover -s tests -v`, `bash -n install.sh config/provision.sh`, and `git diff --check`.
Run `sh -n bootstrap.sh` and test downloads with fake curl in a temporary home. Do not execute the published bootstrap during local tests.
Use temporary homes and fake executables for maintenance tests. Never run them against a live VM without approval.
Inspect CLI help and run Bash and Zsh PATH tests. Report missing shells as missing coverage.
Validate `config/lima.yaml` with the supported Lima version when available.
Distinguish mocked tests, actual read-only observations, live job execution, and disruptive tests.
Passing doctor checks does not establish that the guest is uncompromised.
