# Register and verify a runner

This guide connects one trusted repository to the VM created by the README setup command.
Do not register this public project's runner on the VM.
Use a private target repository whose contributors and workflows you trust.
Complete the README setup first and open a new terminal so `ci-vm` is on PATH.
An adopted runner that is already registered does not need this registration procedure again.

## Check the VM

On the Mac, select the Lima home created by the installer.
If you adopted a VM, use its recorded Lima home instead.

```sh
export LIMA_HOME="$HOME/.local/share/github-runner-vm/lima"
limactl list ci
ci-vm status
```

The VM should be running. The runner is not yet registered or enabled.
The administrator account is `limaadmin`. Jobs must run as `ci`, UID 1001.
Open a guest terminal and switch to the CI account before downloading or running the runner.

```sh
limactl shell ci
sudo -iu ci
id
docker info --format '{{json .SecurityOptions}}'
```

Expect UID 1001, only the `ci` group, and `rootless` in Docker's security options.
If these checks fail, stop and inspect provisioning. Do not grant sudo or use rootful Docker to get past an error.
Before registration, have the guest administrator review and apply available security updates while the new runner is still paused.
Package updates are not automatic. Follow [guest maintenance](maintenance.md#update-the-guest-and-runner), then repeat these checks.

## Download the Linux ARM64 runner

In the target repository on GitHub, open **Settings**, **Actions**, **Runners**, then **New self-hosted runner**.
Choose **Linux** and **ARM64**.
Repository administrator access is required. [GitHub registration instructions](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).

In the guest `ci` terminal, use the existing empty directory.

```sh
cd /home/ci/actions-runner
```

Run the **Download** commands shown by GitHub in that directory.
Verify the archive using the SHA-256 value shown by GitHub before extracting it.
Do not use x64, macOS, or an unverified archive.
Record the selected runner version in your private operations notes.
The current version comes from GitHub, not a copied token or stale version in this guide.

If registration files already exist, stop. Do not overwrite them or use `--replace`.
Keep `.runner*`, `.credentials*`, `.env`, `.path`, and `_diag/` inside the guest.

## Register with a short-lived token

Copy the short-lived registration token from GitHub's setup page.
Do not copy a personal access token, SSH private key, browser profile, or Mac `gh` configuration into the VM.
Registration tokens expire after one hour. [GitHub runner token API](https://docs.github.com/en/rest/actions/self-hosted-runners#create-a-registration-token-for-a-repository).

In the guest `ci` terminal, replace `OWNER/REPOSITORY` with the intended repository.
Run this without `--token` so the runner prompts for the token instead of recording it in shell history.
Paste the token into that prompt yourself. Do not put it in an agent message or saved transcript.

```sh
./config.sh --url https://github.com/OWNER/REPOSITORY \
  --name spare-mac-arm64 --labels spare-mac --work /home/ci/work/actions
```

Use the default runner group. Keep the default `self-hosted`, `Linux`, and `ARM64` labels.
Do not use `--ephemeral`. This is a persistent registration whose listener exits after each job.
It does not provide a clean VM for each job.

Do not run `svc.sh install`, `runsvc.sh`, or a second listener.
The provided user service launches `Runner.Listener run --once` and uses a pause gate between listeners.
Exit the `ci` terminal and then the guest terminal to return to the Mac.

```sh
exit
exit
```

## Enable the provided service

This changes only the new VM's CI user service. It must not be applied to an existing runner without a reviewed migration.
The unit already exists at `/etc/systemd/user/ci-vm-runner.service` in a newly provisioned VM.

```sh
limactl shell ci sudo runuser -u ci -- env \
  XDG_RUNTIME_DIR=/run/user/1001 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus \
  systemctl --user daemon-reload
limactl shell ci sudo runuser -u ci -- env \
  XDG_RUNTIME_DIR=/run/user/1001 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus \
  systemctl --user enable ci-vm-runner.service
ci-vm resume
ci-vm status
ci-vm doctor
```

Check GitHub's runner page. Expect `spare-mac-arm64` to become online.
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

Open and review [examples/smoke.yml](../examples/smoke.yml). Copy its contents to `.github/workflows/runner-smoke.yml` in the **target** repository.
You can use GitHub's file editor; a local copy of this toolkit is not required.
Keep the example outside this public project's active workflows.
If you chose another runner name or label, update both `EXPECTED_RUNNER` and `runs-on` in the example.
All labels in `runs-on` must match the registered runner.

The example has only `workflow_dispatch`, read-only permissions, and no checkout or deployment step.
It verifies identity and rootless Docker without pulling an image or creating containers.
It does not test the target project's dependencies or container networking.

Review and publish that workflow through the target repository's normal process.
Do not add `pull_request_target`, check out fork code, or enable automatic public fork execution.
The workflow file must be on the default branch before manual dispatch is available.

On the Mac, authenticate GitHub CLI if needed. Authentication stays on the Mac.

```sh
gh auth login --hostname github.com
gh workflow run runner-smoke.yml --repo OWNER/REPOSITORY --ref main
gh run list --repo OWNER/REPOSITORY --workflow runner-smoke.yml --event workflow_dispatch --limit 5
```

Use the actual default branch if it is not `main`.
Select the run you just dispatched by its branch, time, and commit. Do not assume the newest unrelated run is yours.

## Verify the whole path

Replace `RUN_ID` and `RUNNER_ID` with IDs returned by GitHub.
The runner-list query is read-only. It can require repository administration permission.

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
- `ci-vm status` and `ci-vm doctor` report the current VM state without unresolved errors.

Then run the target project's real checks on a reviewed branch.
A smoke job does not prove that its database cleanup, cancellation behavior, artifacts, or architecture-specific dependencies work.

Before depending on unattended operation, separately approve and test cooperative pause, resume, guest restart, package updates, and a Mac cold boot.
Test a listener crash with a surviving child to verify that `ExitType=cgroup` prevents a second listener.
Verify controlled host and private-network endpoints from the CI account and from rootless containers.
Those tests create processes or network traffic and are not part of read-only doctor checks.
Record anything untested as untested. Do not infer a cold-boot result from a guest restart.
