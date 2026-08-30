# VM and runner execution reference

Use this reference to execute the approved agent plan from `docs/llm-setup.md`.
The agent runs routine commands. The user completes only GitHub's browser approval when host `gh` is not already authenticated.
An approved plan covers its named steps without repeated confirmations. This reference is also installed with `ci-vm`.
Use a trusted target repository, not this public toolkit.
Read [the security boundaries](security.md) before registration.

If your VM already exists, skip creation. Preserve an existing registration and use the adoption section instead.

## Check the prerequisites

Use an Apple Silicon Mac with macOS 13.5 or newer, Lima 2.2 or newer, Python 3.10 or newer, and GitHub CLI for verification.
You need administrator access to the target repository to register its runner.
For Homebrew's supported path, use macOS 14 or newer and follow [Homebrew's installation instructions](https://docs.brew.sh/Installation).
The user completes administrator prompts. If GitHub CLI needs authentication, the agent launches web login and the user approves it in the browser.

```sh
brew install lima python gh
df -h "$HOME"
```

Check the available disk space before creation. Account for image downloads, job data, all other VMs, and space for macOS.
Do not create a VM on a nearly full disk. The installer does not certify host capacity.
The default allocation is 2 CPUs, 2 GiB RAM, and a 20 GiB virtual disk limit.
Increase it for the target's actual jobs. See [resource planning](maintenance.md#resource-planning).

## Create a repository VM

From a reviewed checkout or extracted source archive, run:

```sh
bash install.sh --repo OWNER/REPO --provision --yes-create-vm --configure-shell
```

This creates one new VM with a deterministic repository-specific name.
It records that repository's profile and adds PATH entries to future Bash and Zsh sessions.
It does not register the runner or change GitHub workflows.
Use `--cpus`, `--memory`, and `--disk` to select integer creation limits. Memory and disk values are GiB.
For example, add `--cpus 4 --memory 8 --disk 60` for a reviewed workload that needs those resources.

For a no-clone install, inspect the published source first, then run:

```sh
(
  set -o pipefail
  curl -fsSL --proto '=https' --connect-timeout 10 --max-time 60 \
    https://raw.githubusercontent.com/builtbycalvin/github-runner-vm/main/bootstrap.sh \
    | sh -s -- --repo OWNER/REPO --provision --yes-create-vm --configure-shell
)
```

The pipeline executes this project's installer on your Mac. It trusts the current `main` branch.
For an unpublished branch, use its reviewed checkout. A bootstrap URL on `main` cannot access unpushed source changes.
For a fixed revision, pin both the bootstrap URL and `CI_VM_REF` to the same reviewed full SHA as shown in [the update reference](maintenance.md#update-the-local-command).
Bootstrap removes its temporary source directory. The installed command retains the setup, maintenance, security, and smoke-example files.

A repeated provision request never replaces an existing VM. A timeout keeps the new profile reservation for inspection. After a definite start failure, the installer removes that reservation only when fresh Lima inventory proves that no VM or instance path exists and the profile still exactly matches the bytes it wrote.
Inspect the saved profile and existing instance before another operation. Use adoption for an approved software update.

Continue in the current terminal with the absolute launcher:

```sh
"$HOME/.local/bin/ci-vm" profiles
"$HOME/.local/bin/ci-vm" setup OWNER/REPO
"$HOME/.local/bin/ci-vm" --repo OWNER/REPO status
```

Future Bash and Zsh sessions can use `ci-vm` after `--configure-shell` updates PATH.
`--configure-shell` preserves existing startup-file text and does not duplicate its marked block on reruns.
Omit it if you manage PATH yourself. See [PATH setup and removal](maintenance.md#shell-path-setup).

## Adopt or inspect an existing VM

Identify its exact Lima home, VM name, guest account, and service before adopting it.
Adoption installs the local command and records identity. It does not start, repair, resize, or reconfigure the VM.
For a repository that does not already have a managed profile, use the actual VM name and Lima home:

```sh
bash install.sh --repo OWNER/REPO --adopt EXISTING_VM --lima-home /absolute/lima-home
```

A VM already claimed by another profile or the legacy installation cannot be claimed again through adoption.
Use the explicit sharing procedure for a supported repository profile. It preserves the existing registration and adds a separate one.
An existing unassigned legacy installation remains selectable with `ci-vm --legacy status`.
For its setup reference, use `ci-vm --legacy setup OWNER/REPO`. This does not assign that legacy VM to a new profile.
Do not overwrite its configuration or relabel it implicitly. Resolve legacy migration as a separate reviewed operation.
If its account or service differs, specify `--guest-user`, `--guest-uid`, and `--unit`.
Maintenance commands refuse incompatible services. Use [the adoption reference](maintenance.md#adopt-without-changing-existing-behavior).

The older single-VM installation form remains supported for an unmanaged existing VM:

```sh
(
  set -o pipefail
  curl -fsSL --proto '=https' --connect-timeout 10 --max-time 60 \
    https://raw.githubusercontent.com/builtbycalvin/github-runner-vm/main/bootstrap.sh \
    | sh -s -- --adopt ci --configure-shell
)
```

## Share an existing repository VM

Use this path only when the user explicitly requests sharing and every affected repository trusts the others.
Check workflow dependencies, fixed ports, Docker resource names, cleanup behavior, credentials, and capacity first.
Shared jobs may run concurrently. The toolkit does not serialize them or isolate one repository from another.
Use the original repository profile that owns the VM as the sharing anchor, not an added member.
It must use the supplied service contract. Legacy and custom-service anchors need separate migration review.

Pause the existing repository before attachment. If it already shares its VM, include `--all-repos` and ensure the user's grant covers all members.

```sh
ci-vm --repo OWNER/FIRST pause
bash install.sh --repo OTHER_OWNER/SECOND --share-with OWNER/FIRST
ci-vm setup OTHER_OWNER/SECOND
```

Attachment requires an already running VM and complete paused-idle proof. It writes a local reservation without creating or resizing a VM.
Repeating the exact attachment preserves the reservation. Do not delete or repoint it after a timeout or incomplete setup.
The existing profile and registration remain unchanged. The second repository needs a fresh registration in its own directory.

Set `VM_NAME`, `RUNNER_KEY`, the unit name, and both directories from the setup output.
Set `LIMA_HOME` to that profile's exact recorded path and export it before any `limactl` command. A VM name alone is not a complete identity.
Copy only the reviewed helper, runtime probe, and service template from this installed toolkit into a new temporary guest directory:

```sh
export LIMA_HOME
SHARED_STAGE="$(limactl shell "$VM_NAME" -- mktemp -d /tmp/ci-vm-shared.XXXXXX)"
limactl cp "$HOME/.local/share/github-runner-vm/config/prepare-shared-runner.sh" \
  "$HOME/.local/share/github-runner-vm/config/container-runtime-state.sh" \
  "$HOME/.local/share/github-runner-vm/config/ci-vm-runner@.service" \
  "$VM_NAME:$SHARED_STAGE/"
limactl shell "$VM_NAME" -- sudo bash "$SHARED_STAGE/prepare-shared-runner.sh" prepare "$RUNNER_KEY"
```

The helper holds a guest lock, verifies paused idle state, and keeps a persistent setup gate before writing guest state.
It prepares the exact member unit and directories without replacing another runner's files. It does not download or register a runner.
If interrupted, retain the temporary directory and gate. Rerun the same reviewed helper for the same key after inspecting the failure.
Do not remove locks, overwrite differing units, or clear the gate manually to continue.

As `ci`, download and verify the official ARM64 runner in the member's directory shown by `setup`.
Then use the second repository's selected profile:

```sh
ci-vm --repo OTHER_OWNER/SECOND register --all-repos
```

Follow the same authenticated registration rules as [the dedicated registration flow](#register-through-authenticated-github-cli).
Do not use the default `/home/ci/actions-runner` or `/home/ci/work/actions` paths for an added member.
Never reuse or copy the first repository's registration files. The selected profile obtains its own short-lived credential through authenticated host `gh`.

Successful `register` performs exact GitHub readback, records the runner ID, enables the inactive member unit, finishes the matching setup gate, and removes its staging directory. The VM remains paused.

Inspect the whole shared VM, then resume every member only under the approved scope:

```sh
ci-vm --repo OTHER_OWNER/SECOND doctor
ci-vm --repo OTHER_OWNER/SECOND resume --all-repos
ci-vm --repo OTHER_OWNER/SECOND status
```

Resume remains a separate authorized action.
A missing member, extra unit, unfinished setup, or package gate blocks VM-wide mutation.
Verify a representative job from each repository on its exact runner before calling sharing ready. Keep results distinct from local profile installation.

## Select the repository VM

`ci-vm setup OWNER/REPO` prints the selected VM and its Lima home. It does not execute commands or verify registration.
Every command below must use that same identity.
For an unassigned legacy installation, use `ci-vm --legacy setup OWNER/REPO` and replace every `ci-vm --repo OWNER/REPO` command below with `ci-vm --legacy`.
Keep that selector throughout setup and maintenance. A missing repository profile is not permission to create or migrate a VM.
Set `VM_NAME` to the exact VM name shown by `ci-vm profiles`.
Use the recorded Lima home if it differs from the default below.
The guest account and service commands assume the supplied `ci` UID 1001 contract.
For a custom adopted account or service, stop and use [the adoption reference](maintenance.md#adopt-without-changing-existing-behavior).

## Check the VM

On the Mac, select the Lima home created by the installer.
If you adopted a VM, use its recorded Lima home instead.

```sh
export LIMA_HOME="$HOME/.local/share/github-runner-vm/lima"
VM_NAME=REPLACE_WITH_VM_NAME
limactl list "$VM_NAME"
ci-vm --repo OWNER/REPO status
```

The VM should be running. A newly provisioned runner is not yet registered or enabled. An adopted VM can already have a registration.
The administrator account is `limaadmin`. Jobs must run as `ci`, UID 1001.
The agent runs bounded guest checks as the CI account before downloading or running the runner:

```sh
limactl shell --tty=false "$VM_NAME" -- sudo -iu ci sh -c \
  'id && docker info --format '\''{{json .SecurityOptions}}'\'''
```

Expect UID 1001, only the `ci` group, and `rootless` in Docker's security options.
If these checks fail, stop and inspect provisioning. Do not grant sudo or use rootful Docker to get past an error.
Before registration, have the guest administrator review and apply available security updates while the new runner is still paused.
Package updates are not automatic. Follow [guest maintenance](maintenance.md#update-the-guest-and-runner), then repeat these checks.

## Download the Linux ARM64 runner

The agent resolves the current official Linux ARM64 runner release and checksum from GitHub. It downloads the archive as `ci` into the empty runner directory shown by `setup`, verifies SHA-256, and extracts it there.
Repository administrator access is required for registration. See [GitHub's registration instructions](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).

Do not use x64, macOS, or an unverified archive.
Record the selected runner version in your private operations notes.
The current version comes from GitHub, not a stale version in this guide.

If registration files already exist, stop. Do not overwrite them or use `--replace`.
Keep `.runner*`, `.credentials*`, `.env`, `.path`, and `_diag/` inside the guest.

## Register through authenticated GitHub CLI

Run the selected profile's registration command. It checks the paused VM, obtains a one-hour token through authenticated host `gh`, sends it only through Lima stdin, configures the exact runner unattended, reconciles local and GitHub identity, records `runner_id`, and enables the selected service without starting it. [GitHub runner token API](https://docs.github.com/en/rest/actions/self-hosted-runners#create-a-registration-token-for-a-repository).

```sh
ci-vm --repo OWNER/REPOSITORY register
```

If authentication is missing, the agent runs `gh auth login --hostname github.com --web`. The user approves access in the browser, then the agent retries. Do not copy a token from GitHub's setup page.

Use the default runner group. Keep the default `self-hosted`, `Linux`, and `ARM64` labels.
Do not use `--ephemeral`. This is a persistent registration whose listener exits after each job.
It does not provide a clean VM for each job.

Do not run `svc.sh install`, `runsvc.sh`, or an untracked listener.
In a shared VM, each repository uses only its own managed service and registration directory.
The provided user service launches `Runner.Listener run --once` and uses a pause gate between listeners.
The command returns only after exact GitHub readback and inactive service enablement succeed. It never starts the listener; `resume` remains explicit.

### Legacy manual fallback

For an unassigned legacy profile, use `ci-vm --legacy register --manual-token OWNER/REPOSITORY` from your own terminal. This fallback is not part of managed repository onboarding. Its deadline leaves guest registration state unconfirmed and the VM paused.

## Verify, then resume the provided service

`register` already enabled the exact selected unit without starting it and confirmed the GitHub runner remained offline and idle. For a shared member, it also completed only the matching registration gate. The VM remains paused.

For a dedicated VM:

```sh
ci-vm --repo OWNER/REPO doctor
ci-vm --repo OWNER/REPO resume
ci-vm --repo OWNER/REPO status
```

For a shared VM:

```sh
ci-vm --repo OWNER/REPO doctor
ci-vm --repo OWNER/REPO resume --all-repos
ci-vm --repo OWNER/REPO status
```

Check GitHub's runner page. Expect the deterministic runner name reported by `register` to become online.
An online runner is not yet proof that the workflow uses it.

## Install job dependencies

The VM includes Docker with Buildx and Compose, Git, curl, jq, unzip, and the runner's basic Ubuntu libraries.
It does not include every tool available on GitHub-hosted Ubuntu.

Keep dependency installation in the target workflow where possible.
Choose ARM64-compatible actions and binaries. Pin action commits and verify downloaded tool archives.
The CI account cannot run `apt-get` with sudo.
Install required system packages through a separately reviewed administrator maintenance step while paused.
Install user tools under `/home/ci/.local/bin`, which is in the service PATH.
Interactive shell profiles are not the runner's service environment.

For container jobs, use ARM64 or multi-architecture images.
Use the rootless socket at `unix:///run/user/1001/docker.sock`.
Never mount the rootful Docker socket, Mac folders, or administrator SSH files into containers.
Treat data cleanup as target-project logic with exact ownership labels. Do not use a global Docker prune.

## Dispatch a smoke workflow

The agent prepares an exact target-repository diff that adds [examples/smoke.yml](../examples/smoke.yml) as `.github/workflows/runner-smoke.yml`.
Publish it only after separate authorization.
Keep the example outside this public project's active workflows.
If you use different labels, update `runs-on` in the example.
All labels in `runs-on` must match the registered runner.

The example has only `workflow_dispatch`, read-only permissions, and no checkout or deployment step.
It verifies identity and rootless Docker without pulling an image or creating containers.
It does not test the target project's dependencies or container networking.

Review and publish that workflow through the target repository's normal process.
Do not add `pull_request_target`, check out fork code, or enable automatic public fork execution.
The workflow file must be on the default branch before manual dispatch is available.

If host `gh` needs authentication, the agent launches web login and the user approves it in the browser. Authentication stays on the Mac.

```sh
gh workflow run runner-smoke.yml --repo OWNER/REPOSITORY --ref main
gh run list --repo OWNER/REPOSITORY --workflow runner-smoke.yml --event workflow_dispatch --limit 5
```

Use the actual default branch if it is not `main`.
Select the run you just dispatched by its branch, time, and commit. Do not assume the newest unrelated run is yours.

## Verify the whole path

Use the runner ID that `register` persisted. Verify the exact run through the selected profile:

```sh
ci-vm --repo OWNER/REPOSITORY verify-run RUN_ID \
  --expect-sha FULL_SHA \
  --expect-event workflow_dispatch \
  --expect-runner-id RUNNER_ID \
  --job smoke
```

The command requires the selected profile to contain the persisted runner ID and compares it with `--expect-runner-id` before it calls GitHub. It verifies the event, commit, job result, runner ID, runner name, and labels. Name every job allowed to run on that runner with `--job`; an unnamed job on the selected runner makes verification fail. The API queries below are read-only diagnostics and can require repository administration permission.

```sh
gh api --paginate repos/OWNER/REPOSITORY/actions/runners \
  --jq '.runners[] | {id,name,status,busy,labels}'
gh run view RUN_ID --repo OWNER/REPOSITORY --json status,conclusion,headSha,event
gh api --paginate repos/OWNER/REPOSITORY/actions/runs/RUN_ID/jobs \
  --jq '.jobs[] | {name,conclusion,runner_id,runner_name,labels}'
gh api repos/OWNER/REPOSITORY/actions/runners/RUNNER_ID \
  --jq '{id,name,status,busy,labels}'
```

Require all of the following before declaring success.

- The selected run has the reviewed commit and `workflow_dispatch` event.
- Every smoke job succeeded on the intended `runner_id` and `runner_name`.
- The job labels include `self-hosted`, `Linux`, `ARM64`, and `spare-mac`.
- The workflow's guest identity and Docker checks passed.
- `ci-vm --repo OWNER/REPO status` and `ci-vm --repo OWNER/REPO doctor` report the current VM state without unresolved errors.

## Move a real job

An online runner does not automatically replace `ubuntu-latest` in your workflows.
GitHub routes a job only when all of its `runs-on` labels match a runner.
[GitHub runner selection](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow).

Choose one trusted Linux job in the target repository.
Record its current `runs-on` value so you can restore it.
After checking that its actions, tools, and container images support Linux ARM64, change that job's runner selection:

```yaml
runs-on: [self-hosted, Linux, ARM64, spare-mac]
```

Keep all other workflow behavior unless a dependency needs a reviewed ARM64 change.
Preserve events, branch and path filters, permissions, `needs`, job names, concurrency, cancellation, artifacts, and cleanup.
If the workflow uses a runner matrix or reusable workflows, review those callers too. Do not replace the matrix blindly.
Keep Xcode and other macOS jobs on macOS runners.
Do not route untrusted public fork jobs to this persistent VM.

Publish the reviewed change through the target repository's normal process.
Run the target project's real checks on a reviewed branch.
A smoke job does not prove that its database cleanup, cancellation behavior, artifacts, or architecture-specific dependencies work.
In GitHub's **Actions** tab, open that run and check its job logs and conclusion.
Use the job API commands above to confirm the exact runner ID, not just a matching name.

Your normal GitHub workflow triggers now send that job to the VM.
The workflow's checkout step downloads the source into the guest. No Mac folder sharing or manual source sync is required.
When a job stays queued, check `ci-vm --repo OWNER/REPO status`, the GitHub runner page, and all `runs-on` labels.
GitHub does not start a stopped VM or wake a sleeping Mac.

To roll back job routing, restore the recorded `runs-on` value through the same review process.
Let active jobs finish. Do not delete the VM or stop the listener to undo a workflow change.

## Check unattended operation separately

Before depending on unattended operation, separately approve and test cooperative pause, resume, guest restart, package updates, and a Mac cold boot.
Test a listener crash with a surviving child to verify that `ExitType=cgroup` prevents a second listener.
Verify controlled host and private-network endpoints from the CI account and from rootless containers.
Those tests create processes or network traffic and are not part of read-only doctor checks.
Record anything untested as untested. Do not infer a cold-boot result from a guest restart.
