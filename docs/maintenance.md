# Maintain and recover the runner

Use `ci-vm profiles` to find the repository, then `ci-vm --repo OWNER/REPO status` before maintenance.
Use the exact Lima home and VM named in the local configuration.
Repository inspection, logs, and run verification use the selected profile. VM maintenance covers every repository sharing its VM.
With multiple configurations, an unqualified command refuses to choose.
Use `--legacy` to select an older single-VM installation explicitly.
Profiles have dedicated VMs by default. Explicit shared profiles use the same VM, CI account, packages, and Docker daemon.

## Maintain a shared VM

Inspect `ci-vm profiles` and the selected repository's `status` before proposing a change.
Shared pause, resume, restart, and package application require `--all-repos`. The command lists every affected repository.
Shared resume requires the complete group to be paused and idle, even on a repeated request. Pause and wait for every runner to drain before resuming; an already active member is not treated as successful idempotent resume.
Authorization must cover those repositories. A flag does not extend a grant for one repository to its siblings.

```sh
ci-vm --repo OWNER/REPO pause --all-repos
ci-vm --repo OWNER/REPO packages ripgrep --apply --yes --all-repos --json
ci-vm --repo OWNER/REPO restart --all-repos
ci-vm --repo OWNER/REPO resume --all-repos
```

Execute only the approved steps. Package installation does not imply approval for restart or resume.
Each listener can accept one final job while pause is pending. Complete idleness requires every member service and cgroup to be empty, plus no remaining runner processes, containers, or service jobs.
The CLI compares host membership with persistent guest unit files and runtime units. Missing or extra members refuse maintenance even if the selected runner looks idle.
A synchronous partial resume failure restores the pause marker. Already-started listeners drain naturally. A submitted start request is not proof of an online runner.

Adding a repository holds a shared-setup gate until its automatic registration transaction completes.
Do not clear that gate or delete a profile to let another member resume. Finish the exact reserved member with [the shared setup procedure](setup.md#share-an-existing-repository-vm), or prepare a separately approved recovery plan.
All repositories share dependency changes and downtime. Keep the CLI upgraded for every operator of that VM. Old binaries and raw administrator commands do not enforce the new group checks.

## Let the agent handle maintenance

The agent inspects the selected runner, proposes one bounded maintenance plan, and performs its approved steps.
An explicit user grant can skip routine confirmations within that scope. It cannot bypass identity checks, pause gates, runtime permissions, or uncertain state.
The agent runs routine commands and verifies their results. If host `gh` needs authentication, the agent launches `gh auth login --hostname github.com --web` and the user approves it in the browser.
See [the agent runbook](llm-setup.md) for dependency discovery and approval rules.

## Install repository system packages

Use the package command for a reviewed list of Ubuntu dependencies. Keep language tools and project dependencies in the target repository's workflow or setup script.
The preview is read-only. It uses existing package indexes and shows installed versions and the proposed transaction, including transitive changes.

```sh
ci-vm --repo OWNER/REPO packages ripgrep libpq-dev --json
```

After the exact request is approved, pause the runner and wait for complete idle evidence before applying it:

```sh
ci-vm --repo OWNER/REPO pause
ci-vm --repo OWNER/REPO packages ripgrep libpq-dev --apply --yes --json
```

An agent uses `--yes` after conversational approval or within an explicit unattended grant. Without it, apply needs an interactive confirmation and refuses a noninteractive caller.
`--yes` skips only the package confirmation. It does not pause the runner, force an idle check, or authorize another target.
For an exact available version, use `PACKAGE=VERSION`. Do not pass URLs, paths, shell commands, package-source definitions, or apt options.

The helper installs from the guest's configured package repositories. Review those sources and their signing configuration before applying changes.
It does not certify source provenance or make package maintainer scripts safe. Those scripts execute as guest root and can change services.
The helper refuses removals, unsupported transactions, and changes to the protected base Docker packages.
It does not add repositories, refresh indexes, upgrade the whole system, clear package-manager locks, or resume the runner.
For stale indexes or unavailable versions, inspect the cause and obtain any needed approval before an index refresh, then preview again.

The helper rechecks its simulated transaction and paused state before applying changes.
A changed transaction requires a new preview and authorization if it exceeds the approved request.
It reads back installed versions, service-contract checks, idle state, and whether a reboot is required.
Inspect the result and verify the tools as the CI user before resuming. A package receipt is not proof that the repository's tests pass.

Before submitting APT, the helper creates the root-owned `/var/lib/ci-vm/package-maintenance` directory.
The updated CLI refuses resume, restart, and another apply while that gate remains.
Successful version, contract, and paused-idle checks allow the helper to remove its empty gate. Failure or timeout leaves it in place.
Use the same updated CLI for all maintenance; old versions and raw administrator commands do not enforce this gate.

On failure or timeout, keep the runner paused and inspect the current guest operation before retrying.
The package manager may still be running after a host timeout. Do not start another transaction or claim rollback.
For recovery, the agent must establish that no package operation or package-manager lock remains, inspect `dpkg --audit`, and compare installed versions with the approved transaction.
It must verify the service contract and paused idle state again. Package repair, if needed, requires a reviewed recovery plan.
Only after that evidence and authorization may the agent remove the exact empty package-maintenance directory using `rmdir` as the guest administrator.
Do not recursively delete the gate, force an operation past it, or clear it merely because a timeout expired.
If a reboot is required, use a separately authorized paused restart and repeat verification before resume.

## Verify a GitHub run

Use the run ID returned by the approved dispatch, the expected commit and event, the independently identified runner ID, and exact job names.
Do not substitute the latest run or infer the expected runner from the run being checked.

```sh
ci-vm --repo OWNER/REPO verify-run RUN_ID \
  --expect-sha FULL_COMMIT_SHA --expect-event workflow_dispatch \
  --expect-runner-id RUNNER_ID --job smoke --json
```

Repeat `--job` for every required job on this VM. Use the exact expanded names for matrix jobs.
Other jobs may run on GitHub-hosted or macOS runners.
The command requires the selected profile's persisted runner ID to match `--expect-runner-id` before it calls GitHub. It reads the selected attempt's paginated jobs and verifies repository, commit, event, completion, successful conclusions, and the selected jobs' runner identity and labels. The repeated `--job` values are the full allowlist for that runner in this run.
It does not dispatch, access guest identity files, or alter GitHub state.
A pending, skipped, missing, mismatched, or unsuccessful required job is not verified success.
A successful smoke job proves runner connectivity; repeat verification for a representative repository job before claiming workload readiness.

## Resource planning

New repository VMs default to 2 CPUs, 2 GiB RAM, and a 20 GiB virtual disk cap.
This is a starting allocation, not a measured minimum or a guarantee that provisioning and your jobs will fit.
Use the actual workflow to choose capacity. Compilation, browsers, databases, and several container images can exceed these defaults.

Creation accepts bounded integers through `--cpus` (1 to 64), `--memory` (1 to 512 GiB), and `--disk` (8 to 4096 GiB).
The parser's lower bounds are not recommended workload minimums.
For example, after reviewing the target workload and host capacity:

```sh
bash install.sh --repo OWNER/REPO --provision --yes-create-vm --cpus 4 --memory 8 --disk 60
```

Disk is a sparse virtual capacity limit, not the amount of host storage consumed immediately.
Images, dependencies, caches, and workspaces grow over time. Check both guest usage and host free space.
Allow for the sum of running VM memory allocations and enough headroom for macOS.
An idle VM still uses resources. Pausing the runner stops future listener admission; it does not shut down the VM or release its memory allocation.
There is no automatic VM suspension, cache pruning, or resource scheduler.

A profile stores requested creation sizes, not current live utilization.
Adoption and command updates do not resize existing VMs. Resource flags are refused with adoption.
Automatic VM names are bounded against Lima's longest temporary SSH socket path on macOS. Explicit VM names are checked before a profile or VM is created; use a shorter `--provision` name or `--lima-home` if the preflight refuses.
Do not edit profile JSON to resize a disk or change the target VM.
A later resize needs a separate reviewed Lima operation with a paused runner, idle evidence, and a recovery plan.
Do not shrink a disk or delete caches to force a workload into the defaults.
Measure an approved representative job before claiming that a smaller allocation is sufficient.

## Pause without cancelling work

```sh
ci-vm --repo OWNER/REPO pause --timeout 30
```

Pause creates `/var/lib/ci-vm/paused` in the guest.
The provided service checks that gate before starting each `Runner.Listener run --once` process.
An existing listener finishes naturally. The CLI never signals it or its worker.
`ExitType=cgroup` keeps the unit active while descendants remain after the main process exits.

If a listener is already waiting, it may accept one last job.
If it receives no job, pause can remain pending indefinitely.
Exit code 4 means that the bounded wait expired, not that the listener was killed.
The pause request stays in place, and no delayed restart is scheduled.
If the user separately authorized that dispatch, run the reviewed smoke workflow to let the listener finish, then repeat `pause`.
Otherwise keep the pause pending and report it. A reviewed workflow alone is not dispatch permission.

After pause succeeds, review any remaining containers or background work before VM maintenance.
Only the project that owns a resource may decide how to remove it.

```sh
ci-vm --repo OWNER/REPO restart --timeout 120
ci-vm --repo OWNER/REPO doctor
ci-vm --repo OWNER/REPO resume
```

`restart` requires an already paused and idle runner.
It refuses remaining runner processes, service activation, active containers, contract drift, and unknown state.
It uses a normal VM stop and start, never force stop.
It leaves the runner paused so you can verify before reopening the gate.
If the command times out after a VM operation was submitted, inspect status before retrying.
The underlying VM operation may still be completing.

Do not run raw Lima, systemd, or another copy of the tool against the same VM concurrently.
The host command lock serializes cooperating commands in one installation, not every possible administrator.

## Read logs and diagnose errors

```sh
ci-vm --repo OWNER/REPO logs --lines 100
ci-vm --repo OWNER/REPO doctor --timeout 30
```

Logs remain local terminal output. They can contain repository details, URLs, and job output.
Sanitize excerpts before sharing them. Do not save raw logs inside this checkout.

| Symptom | Action |
| --- | --- |
| `ci-vm` not found | Export `~/.local/bin` into PATH and check the Bash or Zsh startup file. |
| Python interpreter missing after an upgrade | Rerun the installer from a reviewed checkout with the same adoption settings. |
| Missing Lima | Install Lima yourself. Do not make the CLI download or install it silently. |
| VM absent | Check `lima_home` and VM name. Do not provision a replacement under an assumed identity. |
| VM stopped | The command leaves it stopped. Explicitly start that existing VM with Lima after approval, then verify before resume. |
| Pause pending | Let the current listener finish. Use the reviewed smoke workflow if it is idle. |
| Maintenance refused | Inspect service drift, remaining processes, active containers, or a pending systemd job. |
| Adopted service unsupported | Keep read-only use or review a service migration. Do not bypass the check. |
| GitHub API failure | Check host authentication and repository administration permission. Unknown is not idle. |
| Runner offline after boot | Check FileVault unlock, Mac sleep, VM state, linger, Docker, the pause gate, and registration. |
| Job stays queued | Check runner availability and every `runs-on` label. GitHub cannot start the VM. |
| Dependency missing | Install ARM64 user tools or request a paused guest-administrator package change. |
| Provisioning failed | Inspect cloud-init locally. Preserve the VM and diagnose the failed step. |

Exit codes are 0 for success, 1 for a failed check or operation, 2 for invalid input, 3 for refused maintenance, and 4 for a timeout or pending pause.
See `ci-vm --help` for the installed command syntax.

## Shell PATH setup

Add `--configure-shell` to an install or adoption command to authorize persistent PATH setup.
Without that flag, no shell startup file is changed.
The installer appends a block marked `github-runner-vm PATH` to `~/.bashrc`, one Bash login file, and Zsh's `.zshrc`.
It uses the first existing Bash login file in this order: `.bash_profile`, `.bash_login`, `.profile`.
If none exists, it creates `.bash_profile`. It does not create a file that hides an existing login file.
For Zsh, it uses the installer environment's `ZDOTDIR` if set, otherwise your home directory.
Only paths inside your home are supported. Symlinks, unsafe permissions, and edited managed blocks are refused without replacement.

Open a new terminal after setup. A subprocess cannot change its parent shell's PATH.
For immediate use, run `"$HOME/.local/bin/ci-vm" --repo OWNER/REPO status`, or run this once in your current terminal:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

If you set `ZDOTDIR` inside another shell startup file, pass that same directory to the installer explicitly.
If custom startup code exits early or later resets PATH, inspect that code before changing it.
For manual setup, place the export in the startup files you manage instead of using the flag.

To undo automatic PATH setup, remove only the block from `# >>> github-runner-vm PATH >>>` through `# <<< github-runner-vm PATH <<<` in each changed file.
Keep the block if other installed tools rely on that bin directory. Never delete the entire startup file.

## Update the local command

Download and review a newer commit or release before installing it. Cloning is optional.
Keep the previous source folder until the update passes tests.
For a dedicated or original anchor profile, rerun `install.sh --adopt` with the **same** VM, Lima home, user, UID, unit, and optional GitHub identity settings.
The installer preserves configuration and refuses conflicting values or unrelated files.
It does not change an installed VM template or service.

For a dedicated or anchor repository profile, use the VM name and Lima home shown by `ci-vm profiles`:

```sh
bash install.sh --repo OWNER/REPO --adopt VM_NAME --lima-home /absolute/recorded/lima-home
ci-vm --repo OWNER/REPO status
```

Omit resource flags. The existing profile's creation metadata stays unchanged.
For an added shared member, repeat its exact attachment command with the original anchor shown by `ci-vm profiles`:

```sh
bash install.sh --repo OTHER_OWNER/SECOND --share-with OWNER/FIRST
ci-vm --repo OTHER_OWNER/SECOND status
```

An exact rerun updates installed command files without recreating the member or changing its registration.
Do not replace `--share-with` with adoption, another anchor, or a sibling member. Do not rerun guest preparation just to update the host command.
Version 2 dedicated/anchor profiles and version 3 shared members live in `~/.config/github-runner-vm/profiles/`.
The older version 1 `config.json` remains separate. No hidden active-profile file is used.
Never hand-edit these formats to claim a VM or remove its membership checks.

For an older unassigned VM named `ci` created with this project's dedicated Lima home, use:

```sh
bash install.sh --adopt ci --lima-home "$HOME/.local/share/github-runner-vm/lima"
ci-vm --legacy status
```

The following bootstrap example updates that legacy configuration. For a dedicated profile, add `--repo OWNER/REPO` and use its exact recorded VM and Lima home.
For a shared member, replace the adoption arguments with its exact `--repo` and `--share-with` arguments above.
You can also update without keeping a source folder. Set `revision` to the full 40-character commit SHA you reviewed, then download the bootstrap from that same revision:

```sh
revision=REPLACE_WITH_REVIEWED_FULL_COMMIT_SHA
(
  set -o pipefail
  curl -fsSL --proto '=https' --connect-timeout 10 --max-time 60 \
    "https://raw.githubusercontent.com/builtbycalvin/github-runner-vm/$revision/bootstrap.sh" \
    | CI_VM_REF="$revision" sh -s -- --adopt ci --lima-home "$HOME/.local/share/github-runner-vm/lima"
)
```

Use your recorded identity settings if they differ. Save the old and new commit SHAs privately for rollback.
Pinning chooses a revision; it is not a signature or independent security check.
For a no-clone rollback, run the bootstrap from the previous reviewed commit with the same adoption settings.

If a new CLI expects another service contract, maintenance remains refused until you review and explicitly migrate that service.
To roll back the local command, rerun the prior reviewed source folder's installer with the same settings.
Do not roll back security fixes blindly or overwrite an unfamiliar configuration schema.

## Update the guest and runner

The template pins its Ubuntu image URL and SHA-256, Docker packages, and provisioning version.
It has no floating fallback image.
Ubuntu package repositories and initial security updates remain time-dependent, so this is reproducible configuration, not a bit-identical disk image.
If the dated image expires, verify a replacement URL and checksum before editing the template.
Do not remove the digest to make provisioning pass.

Package lists refresh automatically, but package updates and automatic reboots are disabled.
Ubuntu updates can restart services even when automatic reboot is disabled. [Ubuntu's service restart behavior](https://ubuntu.com/server/docs/how-to/software/automatic-updates/#service-restarts).
The owner must review security updates regularly and apply them promptly in an approved, paused maintenance window.
Docker packages are held for explicit maintenance.
Check `/var/run/reboot-required`, available apt updates, and disk use while paused.
Confirm `apt-config dump` reports `APT::Periodic::Unattended-Upgrade "0"` before relying on this policy.
Administrator package changes can restart services. Review them before execution, then verify the VM and runner before resuming.
After a Docker upgrade, repeat rootless, AppArmor, network, and workflow tests.
Editing this repository's template does not update an existing VM.
The provisioning marker prevents reruns from silently changing an installed guest.

The GitHub runner normally updates itself. This service invokes the listener directly instead of GitHub's `runsvc.sh` wrapper.
Verify runner update exit and restart behavior on the intended VM before depending on unattended updates.
Repeat the single-job and pause tests after runner changes.
GitHub can stop assigning jobs to outdated runners. [GitHub runner update requirements](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#runner-software-updates-on-self-hosted-runners).

## Adopt without changing existing behavior

An existing normal runner service remains unchanged after adoption.
It cannot safely be stopped solely because GitHub reported `busy: false`.
Use read-only commands until you approve a maintenance window and service migration.

A migration plan must inventory the old unit, environment, tool PATH, runner work directory, UID, labels, and startup behavior.
Quiesce workflow producers through an approved repository process, verify no assigned or executing jobs, and review a controlled stop separately.
Do not silently disable workflows or assume API idle state closes the assignment race.
Preserve the registration and do not create another runner against the same Docker daemon.

Install the provided gate and unit only after that review.
Keep the old unit configuration privately for rollback, disabled so both services cannot run together.
Verify pause, one-job execution, crash behavior, resume, and runner updates on the actual host.
If the existing guest does not match UID 1001 and the provided paths, it needs a separate reviewed design rather than a partial adaptation.

For workflow migration, preserve events, schedules, path filters, job names, required checks, permissions, dependencies, concurrency, cancellation, artifacts, and cleanup.
Change only reviewed jobs and their needed dependency setup.
Keep macOS jobs on macOS and public fork checks on GitHub-hosted runners.
Validate with real Actions runs and exact runner IDs.

To roll back a workflow migration, restore its prior `runs-on` and hosted dependency steps through code review.
Remove VM-only preflights and restore architecture-appropriate bootstrap behavior.
Do not delete the VM, registration, or cached data as part of that code rollback.

## Handle sleep and cold boots

The Mac, VM, and runner must remain available.
GitHub does not wake the Mac or boot a stopped VM.
FileVault can require a person to unlock the Mac after a cold boot.
Closing a laptop lid, battery operation, power loss, or sleep can interrupt availability.

Boot autostart and power settings are optional administrator changes. This installer does not set them.
Review `limactl autostart --help` and `pmset -g custom` on the actual Mac.
If you enable boot autostart, confirm that it uses the correct account and the dedicated Lima home.
Do not disable FileVault or change power settings without the owner's explicit approval.
Test a cold boot independently from a guest restart.

## Uninstall without deleting data

First decide whether you are removing the local command or retiring the runner.
Removing the command does not stop the VM or unregister the runner.
The launcher and installed source are shared by all profiles. Keep them if any remaining repository needs the command.
Removing a single profile is a separately reviewed local file deletion, not a runner retirement or disk cleanup.
Record the exact repository, VM, and Lima home privately before removing only that profile's JSON file.
For a shared member, first complete a separately reviewed retirement of its registration and inactive guest unit.
Deleting only its host profile leaves a guest member that maintenance correctly reports as unexpected.
Do not remove an anchor while other profiles reference it. The CLI does not automatically promote a new anchor or remove members.

After confirming the exact files belong to this installation, remove only `~/.local/bin/ci-vm` and these installed files under `~/.local/share/github-runner-vm`:

- `ci_vm.py` and `ci_vm_checks.py`
- `config/lima.yaml`, `config/provision.sh`, and `config/ci-vm-runner.service`
- `config/ci-vm-runner@.service` and `config/prepare-shared-runner.sh`
- `docs/setup.md`, `docs/llm-setup.md`, `docs/maintenance.md`, and `docs/security.md`
- `examples/smoke.yml`

Leave other files in those directories untouched.
Remove `~/.config/github-runner-vm/config.json` only after recording its VM identity privately.
Do not remove the whole share directory. The default VM disks live in its `lima/` child directory.
Leave shared shell PATH entries in place if other tools use `~/.local/bin`.

To retire the runner, obtain approval for that exact repository registration.
Pause and verify the runner first, then remove its registration through GitHub's runner settings.
Disable the now-inactive guest service. Do not use `disable --now` on an active runner.
Keep data unless the owner separately authorizes deletion.

Deleting a VM, its Lima home, Docker volumes, workspaces, or caches is a separate destructive operation.
List exact targets and obtain approval first. Never use a broad `docker system prune`, delete the Mac's default Lima home, or remove unrelated Docker data.
