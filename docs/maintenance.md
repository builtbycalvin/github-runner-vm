# Maintain and recover the runner

Use `ci-vm status` before maintenance.
Use the exact Lima home and VM named in the local configuration.
The commands do not manage other VMs, Docker resources, or repositories.

## Pause without cancelling work

```sh
ci-vm pause --timeout 30
```

Pause creates `/var/lib/ci-vm/paused` in the guest.
The provided service checks that gate before starting each `Runner.Listener run --once` process.
An existing listener finishes naturally. The CLI never signals it or its worker.
`ExitType=cgroup` keeps the unit active while descendants remain after the main process exits.

If a listener is already waiting, it may accept one last job.
If it receives no job, pause can remain pending indefinitely.
Exit code 4 means that the bounded wait expired, not that the listener was killed.
The pause request stays in place, and no delayed restart is scheduled.
Dispatch the reviewed smoke workflow to let the listener finish, then repeat `pause`.
Do not dispatch unreviewed work just to drain a listener.

After pause succeeds, review any remaining containers or background work before VM maintenance.
Only the project that owns a resource may decide how to remove it.

```sh
ci-vm restart --timeout 120
ci-vm doctor
ci-vm resume
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
ci-vm logs --lines 100
ci-vm doctor --timeout 30
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
For immediate use, run `"$HOME/.local/bin/ci-vm" status`, or run this once in your current terminal:

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
Rerun `install.sh --adopt` with the **same** VM, Lima home, user, UID, unit, and optional GitHub identity settings.
The installer preserves configuration and refuses conflicting values or unrelated files.
It does not change an installed VM template or service.

For a VM created with this project's default setup, the update command is:

```sh
bash install.sh --adopt ci --lima-home "$HOME/.local/share/github-runner-vm/lima"
ci-vm status
```

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

After confirming the exact files belong to this installation, remove only `~/.local/bin/ci-vm`, the installed `ci_vm.py`, and its installed `config/` directory under `~/.local/share/github-runner-vm`.
Remove `~/.config/github-runner-vm/config.json` only after recording its VM identity privately.
Do not remove the whole share directory. The default VM disks live in its `lima/` child directory.
Leave shared shell PATH entries in place if other tools use `~/.local/bin`.

To retire the runner, obtain approval for that exact repository registration.
Pause and verify the runner first, then remove its registration through GitHub's runner settings.
Disable the now-inactive guest service. Do not use `disable --now` on an active runner.
Keep data unless the owner separately authorizes deletion.

Deleting a VM, its Lima home, Docker volumes, workspaces, or caches is a separate destructive operation.
List exact targets and obtain approval first. Never use a broad `docker system prune`, delete the Mac's default Lima home, or remove unrelated Docker data.
