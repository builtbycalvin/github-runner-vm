# Preview and install repository dependencies

Users review an exact guest package transaction and, after authorization, apply it while the runner stays paused, with persistent failure gates and verified installed versions.

## Sub-features

- `preview`: exact package names/versions and a non-mutating transaction receipt.
- `confirm`: interactive confirmation or explicitly authorized `--yes`.
- `apply`: paused/idle checks, pinned transaction, version readback, retained pause.
- `failure`: refusal or timeout leaves incomplete work blocked for inspection.
- `shared`: the package transaction changes the whole guest, so application requires `--all-repos` and paused-idle proof for every member.

## How to get to it (user POV)

- `ci-vm --repo verify/one packages ripgrep` and the same command with `--json`.
- `ci-vm --repo verify/one packages ripgrep --apply`.
- `ci-vm --repo verify/one packages ripgrep --apply --yes --json`.
- For an explicitly shared VM, add `--all-repos` to application only after approval from the user for the complete affected scope. Preview needs no scope override.

## Driving it with verify_cli.py

Preconditions: default stopped fixtures cannot run APT. The following is regression evidence only. Live installation needs an approved package list/source and an already running, paused, idle supported guest.

- **Safety regressions.** Run `"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature packages`. Read `checks-packages.terminal.txt`: request parsing, preview without mutation, noninteractive confirmation, exact version readback, transaction drift, busy-state refusal, and persistent failure gates must pass.
- **Proof boundary.** These tests use in-process guest responses. They do not establish actual APT behavior or exercise typing `yes` in a real terminal. Report those gaps explicitly.
- **Authorized guest proof.** Follow `docs/maintenance.md` and the dependency evidence procedure in `docs/llm-setup.md`. Observe the exact requested/resolved transaction, confirmation branch, post-install versions, gate state, and paused state. Observe that preview performs no install or index refresh; do not trust the word preview alone. Capture sanitized JSON receipts; do not save credential-bearing APT source URLs or raw logs.

## Gotchas

- `--yes` expresses an authorized decision; it cannot grant permission or override guards.
- A package-manager exit 0 is insufficient without exact installed-version and idle readback.
- Timeout may leave guest work running. Never remove locks or clear the package gate simply to retry.
- A pending shared runner setup also blocks package application. No mutation should cross that gate, including after the initial preflight.
- These commands do not infer all project dependencies, refresh indexes, certify package provenance, or automatically resume CI.
