# github-runner-vm

Give your AI agent a repository. It sets up a Linux VM on your Apple Silicon Mac for that repository's GitHub Actions jobs.
GitHub keeps the triggers, logs, and checks. Your Mac runs the jobs.

## Set it up through your agent

```text
Configure this Mac for OWNER/REPO with https://github.com/builtbycalvin/github-runner-vm.
```

The agent follows [the agent runbook](docs/llm-setup.md). It inspects your Mac and repository, identifies dependencies, and proposes the changes in one plan.
You approve the plan once. The agent performs the approved steps, checks the results, and reports any remaining work.
It does not ask you to copy routine commands or decide which packages your workflows need.

To skip routine confirmations, add explicit permission:

```text
For this setup, proceed without routine confirmations. You may install the
required host tools and repository dependencies, create one dedicated VM,
configure its runner service, and run local verification. Do not delete or
replace existing VMs, change host security or power settings, publish changes,
or dispatch GitHub workflows without separate permission.
```

This permission applies to this repository and this setup run. It does not disable tool permissions, identity checks, or maintenance safeguards.
The agent still shows its plan and results. A new target or action outside that scope needs permission.
If GitHub CLI is already authenticated, runner registration needs no extra human step. Otherwise the agent launches `gh auth login --hostname github.com --web`; browser approval is the only GitHub credential step. Host installation may still show an administrator prompt. Never paste credentials into the conversation.
If you want the agent to publish a reviewed workflow and run it, explicitly include that repository and those actions in your approval.

**Using an unpublished branch?** Supply its branch URL or reviewed local checkout.
The default repository URL selects `main`, which cannot install unpushed changes.

## Dependencies follow the repository

The agent reads workflow steps, referenced scripts, lockfiles, version files, and container definitions.
It reports the required versions, the source of each requirement, and anything it cannot establish.

Host tools belong on the Mac. Ubuntu packages belong in the VM. Language tools and project dependencies stay reproducible in the target repository's workflows or setup scripts.
Jobs run without sudo. The toolkit supplies Ubuntu ARM64, rootless Docker, Git, basic utilities, and runner libraries, not every GitHub-hosted toolchain.

A dependency installation is verified through installed-version readback and checks as the CI user.
A smoke job verifies the runner connection. A representative repository job verifies the actual workload.
The agent must distinguish those results.

## Dedicated by default, shared when you choose

By default, each repository has its own VM, runner registration, Docker daemon, workspaces, and dependency state.
The VM persists across jobs. The agent never silently reuses another repository's VM.
Existing legacy installations remain available without automatic migration.

To reuse a supported repository VM, tell the agent:

```text
Configure OWNER/SECOND on this Mac using the VM already set up for OWNER/FIRST.
Check that their dependencies and workloads can share it, then ask once before applying the plan.
```

The agent uses `--share-with` after checking compatibility and pausing the existing runners.
Each repository gets a separate runner registration and workspace, including repositories owned by different personal accounts.
They share the CI account, packages, Docker daemon, ports, and VM resources. Their jobs may run concurrently.
Share only mutually trusted repositories whose workflows tolerate those shared resources. Separate directories do not isolate their credentials or files.
Registration, pause, resume, restart, and package changes affect every repository in that VM. The agent names them before acting and uses `--all-repos` to acknowledge the scope.
Adding a repository keeps the shared VM paused until its setup and registration are complete.

New VMs start with 2 CPUs, 2 GiB RAM, and a 20 GiB virtual disk cap.
These are provisional defaults, not proven minimums for every workload.
Agents can choose creation sizes through `--cpus`, `--memory`, and `--disk` after inspecting the workflow and host capacity.
Adoption never resizes a VM. Sparse disks grow with use, and each running VM consumes resources even when its runner is paused.

Use trusted code, preferably in private repositories. Do not automatically send public fork jobs to this persistent runner.
Xcode and other macOS jobs stay on macOS runners.

## Maintain it through your agent

```text
Use github-runner-vm to check the runner for OWNER/REPO on this Mac.
Explain any needed maintenance and ask once before applying the plan.
```

You can authorize a named maintenance plan without repeated confirmations.
The agent uses the selected repository profile, identifies every affected repository, waits for a cooperative pause, performs only the approved maintenance, and verifies before resuming.
A pending pause or uncertain result stops the operation. There is no force mode, automatic deletion, or blanket approval saved in repository files.

## Agent references

- [Setup, dependency discovery, approval scope, and verification](docs/llm-setup.md).
- [VM and runner execution reference](docs/setup.md).
- [Maintenance and resource planning](docs/maintenance.md).
- [Security boundaries](docs/security.md).
- [Agent walkthrough checks](docs/agent-walkthrough.md).

The installed `ci-vm` command includes its operating references. Agents use the same tested commands for every repository.
The public toolkit keeps its own CI on GitHub-hosted runners.

Licensed under the [MIT License](LICENSE).
