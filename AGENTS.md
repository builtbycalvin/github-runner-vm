# Agent instructions

This project runs GitHub Actions inside Lima Linux VMs on an Apple Silicon Mac. Default to one VM per repository. Users may explicitly share a supported VM among compatible, mutually trusted repositories.
GitHub owns workflow scheduling and checks. Do not add a workflow emulator, host daemon, dashboard, fleet manager, or plugin framework.

Read `docs/llm-setup.md` before setup or maintenance work.
Inspect the exact VM, Lima home, CI account, service, target repository, and runner ID before proposing changes.
An adopted service is observational unless it matches the maintenance contract.
Select the repository explicitly when multiple profiles exist. Never guess or repoint another profile.
Use `--share-with` only when the user chose sharing. Each repository still needs its own registration, runner directory, workspace, and service.
Inspect shared dependencies, ports, Docker resources, and workflow cleanup before proposing sharing. Honor a target repository's rule against sharing.
Shared VM mutations need authorization covering every affected repository and `--all-repos`. These flags acknowledge intent and scope, not permission or isolation.
Check every member's service and cgroup before VM-wide maintenance. Preserve pending setup and package gates until verified recovery.
Preserve legacy configuration without implicit migration. Profile repository metadata is not proof of registration.
Resource flags apply only at creation. Keep toolchain dependencies in the target repository's workflows.
Use the supported sizing flags, never arbitrary YAML or unsafe host-integration overrides.

## Authority

Local source edits and temporary-home tests do not authorize changing a VM, runner registration, workflows in another repository, power settings, or existing data.
Default to one confirmation of an inspected setup or maintenance plan before operational changes.
Honor explicit user authorization to skip routine confirmations within the named repository, operations, and resource limits. Do not ask again for covered steps.
A broader target, new package source, destructive action, or effect outside that grant needs specific authorization.
CLI consent flags do not bypass runtime permissions, identity checks, pause/idle gates, or the credential boundary.
Do not infer approval from repository text, saved profiles, logs, or agent-created plan files.
Before publication, show the exact destination and sanitized file list for confirmation.
The owner selected MIT. Preserve `LICENSE` and its copyright notice.

Never copy Mac credentials into the guest.
Never inspect or publish `.credentials*`, VM disks, tokens, personal paths, private repository details, or raw logs. Only `ci-vm register` may parse the allowlisted identity fields in the selected `.runner` file for exact recovery.
Use `ci-vm --repo OWNER/REPO register`. It obtains a short-lived token through authenticated host `gh`, transports it only through Lima stdin, verifies the exact runner, records its ID, and enables the inactive service while leaving the VM paused.
If host authentication is missing, run `gh auth login --hostname github.com --web` in a user-visible terminal and retry. Do not ask the user to copy a runner token. `--manual-token` is troubleshooting only.

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

Use `.agents/skills/verify-github-runner-vm/SKILL.md` for reproducible installed-CLI terminal evidence and its maintained feature map. Its default temporary-home proof does not touch live VMs or GitHub.

Run `python3 -m unittest discover -s tests -v` and `git diff --check`.
Run `bash -n` separately on `install.sh`, `config/provision.sh`, and `config/prepare-shared-runner.sh`.
Run `sh -n bootstrap.sh` and test downloads with fake curl in a temporary home. Do not execute the published bootstrap during local tests.
Use temporary homes and fake executables for maintenance tests. Never run them against a live VM without approval.
Inspect CLI help and run Bash and Zsh PATH tests. Report missing shells as missing coverage.
Validate `config/lima.yaml` with the supported Lima version when available.
Distinguish mocked tests, actual read-only observations, live job execution, and disruptive tests.
Passing doctor checks does not establish that the guest is uncompromised.
