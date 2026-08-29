# Repository profiles

Users get a dedicated VM by default. Explicit sharing adds a separate repository runner to an existing VM without silently migrating the legacy configuration.

## Sub-features

- `inventory`: list all local identities without probing a VM.
- `explicit`: select the repository before or after the command.
- `legacy`: explicitly inspect the unassigned legacy installation.
- `refusal`: reject ambiguous, missing, repeated, or abbreviated selection.
- `persistence`: keep bindings and configuration permissions unchanged during inspection.
- `sharing`: explicitly attach to the original repository anchor with `--share-with`, preserve repeat reservations, and reject implicit reuse or unsupported anchors.

## How to get to it (user POV)

- `ci-vm profiles`
- `ci-vm --repo verify/one status`
- `ci-vm status --repo verify/two`
- `ci-vm status --legacy`
- `ci-vm setup verify/one`, `ci-vm --repo verify/two setup verify/two`, `ci-vm --legacy setup verify/legacy`
- `ci-vm status` when several profiles exist; malformed or unknown selectors must refuse.
- `bash install.sh --repo other/second --share-with verify/one` after approval and paused-idle proof for the complete existing VM.

## Driving it with verify_cli.py

Preconditions: the parent skill's launch and doctor succeeded; three fixture profiles exist. Use the helper, never those commands in your normal HOME.

- **Inventory and guides.** Run `"$CI_VM_VERIFY_HELPER" drive "$CI_VM_VERIFY_RUN" --feature profiles`. The inventory names both repository profiles and legacy; each guide names the selected VM and says setup is not verified. The boundary log is unchanged by these commands.
- **Explicit routing.** The same drive invokes both selector positions and legacy selection. Transcripts show exactly `VM one: Stopped`, `VM two: Stopped`, and `VM legacy: Stopped`, respectively. Exactly three additional `list --json` calls use a Lima home beneath the fixture HOME.
- **Refusals.** The drive also executes missing selector, unknown repository, repeated selector, and `--rep` abbreviation paths. Each returns 2 without an external call.
- **Persistence.** Inspect `evidence/home-before.json`, `home-after.json`, and `drive.json`: hashes, modes, and mtimes match, bindings are `verify/one` → `one` and `verify/two` → `two`, and repository JSON files have mode 600.
- **Additional guard regressions.** Run `"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature profiles` for physical-path aliases, unsafe config, and selector guards. These supplement the recorded PTY paths.
- **Explicit sharing.** On a separate fresh run, use `"$CI_VM_VERIFY_HELPER" drive "$CI_VM_VERIFY_RUN" --feature sharing`. The installer first refuses implicit reuse of VM `one`, then reserves `another/three` with anchor `verify/one`. Inspect `sharing-attach.terminal.txt`, `sharing-rerun.terminal.txt`, and `drive-sharing.json` for exact rerun preservation and the v3 binding. The helper changes only its fake guest inventory to simulate completed preparation.
- **Shared guide and scope.** The same drive checks `sharing-guide.terminal.txt` for the derived member identity and `resume --all-repos`. `sharing-scope-refusal.terminal.txt` must name both repositories and refuse unacknowledged pause. `sharing-pause.terminal.txt` demonstrates the installed command against fake paused/idle guest responses, not a live drain.
- **Sharing regressions.** Run `"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature sharing` for attachment and guest helper recovery tests. Guest helper tests rewrite filesystem roots into a temporary directory and fake platform commands. They do not establish live systemd, flock, cgroup, or registration behavior.

## Gotchas

- Repository metadata is not proof of registration or that an Actions job used this VM.
- A shared Lima home can contain distinct VMs. Do not confuse home reuse with VM reuse.
- A shared profile is a reservation, not a registered runner. Successful `register` performs exact GitHub readback, persists the runner ID, enables the inactive unit, and completes the matching setup gate. A stale or missing member in the guest inventory must block maintenance.
- Sharing requires the original anchor, not another shared member. It does not isolate credentials, packages, Docker, ports, or simultaneous jobs.
- A stopped status is intentionally successful inspection; no VM should start.
- This drive does not prove creation, resizing, or every malicious configuration case. List remaining gaps separately.
