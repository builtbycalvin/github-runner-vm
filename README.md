# github-runner-vm

Turn a spare Apple Silicon Mac into a GitHub Actions runner inside a Lima Linux VM.
GitHub handles triggers, scheduling, logs, and PR checks. The VM executes jobs.
This project installs a small `ci-vm` maintenance command. It does not emulate workflows.

**Use this with trusted code, preferably in a private repository.** A persistent runner keeps files and Docker data between jobs.
Public fork contributions must not automatically execute on it. This public project's own CI uses GitHub-hosted runners only.

## Before you start

You need an Apple Silicon Mac running macOS 13.5 or newer, Lima 2.2 or newer, Python 3.10 or newer, and GitHub CLI for verification.
You do not need to clone the repository or run Git commands to install this tool.
The default VM uses four CPUs, 8 GiB RAM, and an 80 GiB virtual disk that grows as data is written.
You also need administrator access to the target GitHub repository to register its runner.

Before creating a VM, check the `Avail` column for your home disk:

```sh
df -h "$HOME"
```

The VM does not reserve all 80 GiB immediately, but it can grow to that size as jobs and Docker data accumulate.
Plan capacity for that growth plus the downloaded image and space for macOS and your other apps.
Do not provision on a nearly full disk. Free space or use another Mac first; the installer does not currently enforce a free-space minimum.

If you use Homebrew, install the prerequisites yourself in Terminal.
Use macOS 14 or newer for Homebrew's supported installation path.
If `brew` is not found, follow [Homebrew's installation instructions](https://docs.brew.sh/Installation) first, including its printed **Next steps** for PATH.
Homebrew or Apple's command line tools may require administrator approval.
Do not give an agent your administrator password.

```sh
brew install lima python gh
```

## 1. Choose new setup or adoption

### New VM

Run this in Terminal after installing the prerequisites. No clone or manual download is needed.
It authorizes creating a **new** VM and adding PATH entries to your Bash and Zsh startup files.
It does not register a GitHub runner or change workflows.

```sh
(
  set -o pipefail
  curl -fsSL --proto '=https' --connect-timeout 10 --max-time 60 \
    https://raw.githubusercontent.com/builtbycalvin/github-runner-vm/main/bootstrap.sh \
    | sh -s -- --provision ci --yes-create-vm --configure-shell
)
```

The command downloads and executes this project's installer **on your Mac**.
It trusts the current code on this repository's `main` branch. Read [bootstrap.sh](bootstrap.sh), [install.sh](install.sh), [ci_vm.py](ci_vm.py), and [config/](config/) first if you want to inspect it.
The bootstrap downloads a source archive into a temporary directory, calls the same local installer, and removes that temporary copy afterward.
It never installs prerequisites or chooses a VM operation for you.

The installer uses a separate Lima home under `~/.local/share/github-runner-vm/lima`.
It refuses an existing VM with the same name. It never recreates a VM after a failed or repeated install.
If provisioning times out, inspect the existing instance before retrying. A timeout is not a rollback.

The installer copies `ci-vm` into `~/.local/bin` and configures PATH for future Bash and Zsh sessions.
Open a **new terminal window**, then run `ci-vm status`.
To check from the current terminal without reopening it, use:

```sh
"$HOME/.local/bin/ci-vm" status
```

An installer cannot change PATH in the terminal that launched it.
`--configure-shell` adds one marked block, preserves existing text, and does not duplicate the block on reruns.
It updates `.bashrc`, the existing Bash login file, and `.zshrc`. Custom `ZDOTDIR` under your home is supported.
Omit the flag if you manage shell startup files yourself. See [PATH setup and removal](docs/maintenance.md#shell-path-setup).

### Existing VM

Adoption installs the local command and records the existing VM name. It does not provision, start, repair, or reconfigure the VM.

```sh
(
  set -o pipefail
  curl -fsSL --proto '=https' --connect-timeout 10 --max-time 60 \
    https://raw.githubusercontent.com/builtbycalvin/github-runner-vm/main/bootstrap.sh \
    | sh -s -- --adopt ci --configure-shell
)
```

If the VM uses a different Lima home or runner user service, specify them explicitly.

```sh
(
  set -o pipefail
  curl -fsSL --proto '=https' --connect-timeout 10 --max-time 60 \
    https://raw.githubusercontent.com/builtbycalvin/github-runner-vm/main/bootstrap.sh \
    | sh -s -- --adopt ci --lima-home "$HOME/.lima" --unit my-runner.service
)
```

Provide the actual guest user, UID, and user-service name with `--guest-user`, `--guest-uid`, and `--unit` if they differ from the defaults.
`status`, `doctor`, and `logs` inspect adopted user services. `doctor` can report an unsupported service contract without changing it.
`pause`, `resume`, and `restart` require this project's exact maintenance service contract.
They refuse ordinary `runsvc.sh` services because stopping those services can cancel jobs.
Read [Adopt without changing existing behavior](docs/maintenance.md#adopt-without-changing-existing-behavior) before proposing a service migration.

## 2. Register and run your first job

For a new VM, continue with [Register and verify a runner](docs/setup.md).
The VM starts without a registered runner and with its pause gate closed.
The guide walks through downloading the Linux ARM64 runner, entering a short-lived registration token, enabling the service, and running a manual smoke workflow.
Success means GitHub reports a successful job on the exact intended runner ID, not merely an online runner.

For adoption, preserve the existing registration and workflows. Start with read-only checks, not registration again.

## Prefer to inspect a local copy?

Cloning remains supported. Review the files, then run the same installer directly:

```sh
git clone https://github.com/builtbycalvin/github-runner-vm.git
cd github-runner-vm
bash install.sh --provision ci --yes-create-vm --configure-shell
```

Use `--adopt ci` instead of `--provision ci --yes-create-vm` for an existing VM.
A downloaded and extracted source ZIP also works; Git history is not required.
For repeatable setup, check out a reviewed commit before installing.
The bootstrap also accepts `CI_VM_REF` set to a full commit SHA. Pin both the bootstrap URL and the source archive to that same reviewed SHA, as shown in [maintenance](docs/maintenance.md#update-the-local-command).

## Daily use

| Command | Effect |
| --- | --- |
| `ci-vm status` | Read VM and runner service state. Never starts a VM. |
| `ci-vm doctor` | Read prerequisite and isolation checks. Failures need investigation. |
| `ci-vm logs --lines 100` | Show a bounded local journal tail. Treat logs as sensitive. |
| `ci-vm pause --timeout 30` | Request a pause after the current listener exits naturally. |
| `ci-vm resume` | Open the pause gate and start the existing managed service. |
| `ci-vm restart` | Restart an already paused VM only after conservative idle checks. Leave it paused. |

A pause can remain pending. A listener already waiting for work may accept one final job.
If no job arrives, the listener can keep waiting. Dispatch the reviewed smoke workflow to let that listener finish, then run `pause` again.
The command never cancels that job to meet its timeout. A timeout leaves the VM running and the pause request intact.
`restart` refuses active jobs, active Docker containers, unknown state, and incompatible services.
There is no force option.

The installed launcher, code, and configuration live under your own home directory.
See [Maintenance and recovery](docs/maintenance.md) for updates, errors, rollback, and uninstall.
Agents should start with [docs/llm-setup.md](docs/llm-setup.md).

## Availability and limits

- The Mac, VM, and runner must all be available for jobs to execute. GitHub does not boot a stopped VM.
- Sleep, loss of power or network, FileVault unlock, and cold boots affect availability.
- This Linux VM cannot execute Xcode, Simulator, code-signing, or other macOS jobs. Keep those on macOS runners.
- Rootless Docker and network rules reduce exposure. Passing health checks is not proof against compromise.
- Jobs can affect later jobs. Do not mix trust levels or attach production secrets.
- Guest package updates require approved, paused maintenance. Review and apply security updates promptly; they are not installed automatically.
- Linux ARM64 dependencies differ from GitHub-hosted x64 Ubuntu. Review architecture support before moving a job.

Read [Security boundaries](docs/security.md) before registration.
No dashboard, alert service, scheduler, fleet manager, or plugin system is included.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
bash -n install.sh config/provision.sh
sh -n bootstrap.sh
```

Contributors can clone the repository and also run `git diff --check`. That Git check does not apply to a downloaded ZIP.
Tests use fake Lima and GitHub commands in temporary homes. They do not create a VM or register a runner.
Use the [live verification checklist](docs/setup.md#verify-the-whole-path) on the intended host before relying on unattended jobs.
Provisioning, network isolation, service draining, runner updates, VM restart, and Mac cold-boot behavior require separate live verification.

## License

Licensed under the [MIT License](LICENSE).
