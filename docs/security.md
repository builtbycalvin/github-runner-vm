# Security boundaries

This VM is for mutually trusted code. It is persistent and has unrestricted access to public internet destinations over IPv4.
A job can read files owned by the CI user, change later jobs, and steal any credentials made available to that user.
Rootless Docker does not turn a persistent runner into a safe executor for hostile pull requests.
[GitHub's self-hosted runner security guidance](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#hardening-for-self-hosted-runners).

## Accounts and host integration

`limaadmin`, UID 1000, administers the guest with sudo.
`ci`, UID 1001, runs jobs without sudo or membership in the rootful Docker group.
Docker uses a rootless daemon at `/run/user/1001/docker.sock`.
Rootful Docker and containerd services are masked.
AppArmor and Ubuntu's restricted unprivileged user namespaces remain enabled.
The rootlesskit AppArmor profile permits its required user namespace operation.
Rootless Docker does not provide container AppArmor profiles. [Docker's rootless limitations](https://docs.docker.com/engine/security/rootless/troubleshoot/).

The Lima template has no Mac shared folders, extra networks, forwarded SSH agent, loaded personal SSH public keys, inherited proxies, or automatic port forwarding.
Lima still needs its own management connection and management SSH public key for the admin account.
Those are not job credentials. No Mac private key is copied to the guest.
Public DNS is configured explicitly.

Lima global configuration can add mounts, environment variables, or forwarding even when an instance template is restrictive.
New provisioning uses a dedicated Lima home and rejects global default, override, or base files there.
Before any later start or restart, inspect that Lima home's `_config` directory and the installed instance configuration.
Never copy the user's default Lima configuration into it.
Adoption does not certify an existing configuration or remove unsafe settings.
[Lima's configuration merge rules](https://github.com/lima-vm/lima/blob/v2.2.0/templates/default.yaml).

## Network restrictions

Guest nftables applies output restrictions to UID 1001 and its subordinate UID range, 200000 through 265535.
Guest loopback remains available for local database and API tests.
The rules reject common private, loopback-over-network, link-local, carrier-grade NAT, documentation, benchmark, multicast, and reserved IPv4 destinations.
They reject all non-loopback IPv6 traffic from those UIDs.
The exact IPv4 ranges are in `config/provision.sh`.

This reduces access to the Mac gateway and private networks.
It is not a complete network perimeter or a public-internet allowlist.
Public IP addresses can route back to private services, and guest-root or hypervisor compromise can bypass these protections.
The administrator account is not restricted by this UID rule.
Rootless container networking must be tested separately because the outer socket identity determines which rule applies.
VPNs, proxies, additional accounts, changed UID maps, and extra interfaces need a new review.

After provisioning, a human should verify a controlled host endpoint and a controlled private-network endpoint from both `ci` and rootless containers.
Test public HTTPS and guest-loopback services too.
Do not scan arbitrary local networks.
Such probes are not read-only health checks and require approval when an agent runs them.
For stronger isolation, use a separately administered network boundary or an ephemeral execution design.

## Workflow trust

Prefer a private repository with trusted contributors.
Do not automatically run public fork contributions on this persistent runner.
Manual approval does not make malicious code safe.
Do not combine `pull_request_target`, privileged tokens, and checkout of fork code.
Review workflows and third-party actions as executable code. Pin action commits.
Use minimal token permissions and do not give these jobs production secrets.

Default to one repository and one registered runner per VM and Docker daemon.
Explicit `--share-with` adds a separate repository registration, runner directory, work directory, and service to an existing supported VM.
It does not create an organization-scoped runner or expand a repository registration's access. [GitHub runner scopes](https://docs.github.com/en/actions/concepts/runners/self-hosted-runners).
Default `ci-vm register` uses authenticated host `gh`, sends the short-lived token only through Lima stdin as the runner's secret environment input, and removes it from the guest environment during runner initialization. It never stores the token or puts it in process arguments. `--manual-token` is an explicit troubleshooting fallback.
All shared repositories must be mutually trusted. They share the CI account, packages, Docker daemon, ports, caches, resource budget, and access to CI-owned files and credentials.
Separate directories are organizational boundaries, not security boundaries. Jobs from different repositories may run concurrently.
Inspect fixed ports, container names, cleanup commands, and dependency conflicts before offering reuse. Keep incompatible workloads dedicated.
Profiles reject accidental duplicate physical VM bindings and malformed shared references.
They do not make untrusted code safe, and saved repository metadata does not prove a GitHub registration.
Creation sizing flags change only CPU, memory, and disk capacity. They do not permit arbitrary mounts, forwarding, or network settings.
GitHub runs one job per listener, but containers and files can outlive that job.
Runtime checks fail closed when another user or root owns a container daemon, a second runtime socket is present, an unsupported runtime is active, or complete evidence cannot be collected.
Shared ports and global cleanup are still hazards.
The pause gate does not serialize unrelated services or defend against a compromised CI account.
Shared maintenance checks the full managed unit inventory and every member's cgroup. An extra, missing, or changed unit refuses disruptive work.
Each materialized root-owned instance unit remains inventory even when disabled or unloaded. Do not delete a host profile to hide a member.
Shared setup and package gates survive incomplete operations. `--all-repos` acknowledges their VM-wide scope but cannot bypass them.

## Agent approval and dependency execution

The user can approve an inspected plan once or explicitly authorize a bounded unattended run.
The agent owns that scope. A consent flag, repository file, package manifest, saved record, or tool output is not authority.
Confirmation bypass never bypasses runtime permissions, exact repository selection, service-contract checks, or paused-idle requirements.
It does not grant access to secrets or permission to delete, replace, publish, or operate on unrelated repositories.

The package helper uses existing guest package repositories and their configured trust.
The agent must review source and signing changes separately. The helper does not establish that a repository or package is trustworthy.
Package maintainer scripts run as guest root. A reviewed package request includes those effects and its transitive dependencies.
Keep product setup scripts under the CI user, without sudo. Never add a generic privileged script executor to simplify agent setup.
Package apply leaves a root-owned maintenance gate until its readback checks succeed. Updated CLI commands refuse resume and restart while the gate remains.
This coordinates cooperating tools. An older CLI, raw administrator commands, or a compromised guest can bypass it.

## Maintenance is not incident recovery

`doctor` reports observations. It does not prove absence of compromise.
A compromised guest can lie about processes, files, network rules, or service state.
Root ownership of a user-service file does not prevent the CI user from overriding its own service configuration.
Exact unit checks catch accidental drift, not a hostile guest.

If compromise is suspected, stop sending trusted work to that runner and involve its owner.
Revoke exposed credentials and replace the environment through a separately approved recovery plan.
Do not run an automatic cleanup or call a health check proof of recovery.

## Public repository hygiene

Publish source, sanitized examples, tests, and documentation only.
Do not publish local configuration, real runner IDs, runner identity files, logs, archives, disks, host paths, private repository names, or credentials.
The install configuration remains under the operator's home directory.
`.gitignore` is only a convenience. Inspect the actual proposed diff and file list before publishing.
Never run `git add .` after collecting live diagnostics.
