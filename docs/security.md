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

Keep one registered runner per VM and Docker daemon unless a separate isolation design justifies more.
GitHub runs one job per listener, but containers and files can outlive that job.
Shared ports and global cleanup are still hazards.
The pause gate does not serialize unrelated services or defend against a compromised CI account.

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
