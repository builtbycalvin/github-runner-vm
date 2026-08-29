---
name: verify-github-runner-vm
description: Verify github-runner-vm through its installed ci-vm CLI and terminal output. Use after onboarding, profile selection or sharing, CLI presentation, or runner maintenance changes, and when collecting reproducible evidence without touching live VMs or GitHub.
---

# Verify github-runner-vm

Read the repository's `AGENTS.md`, `docs/llm-setup.md`, and [feature index](features/README.md). The primary surface is a short-lived Python CLI with formatted terminal output, not a persistent TUI or server. Secondary surfaces are the shell installer/bootstrap and GitHub Actions results.

The default proof uses the real installer and installed launcher, with fake executables at the existing Lima/GitHub subprocess boundary. It never imports application functions to manufacture the demonstrated profile state. Existing tests supplement this path; in-process mocks are regression evidence, not terminal or live-system proof.

## Launch

Prerequisites: reviewed local checkout, Python 3.10+, `/bin/bash`, and a normal non-root user on macOS or Linux. No pip packages, Lima installation, GitHub authentication, ports, or live VM are required. Zsh is needed for the supplementary onboarding shell checks; report its absence as skipped coverage.

Run from the repository root:

```sh
CI_VM_VERIFY_HELPER="$PWD/.agents/skills/verify-github-runner-vm/scripts/verify_cli.py"
CI_VM_VERIFY_RUN="$("$CI_VM_VERIFY_HELPER" launch)"
printf '%s\n' "$CI_VM_VERIFY_RUN"
```

Launch prints one absolute run directory beneath the OS temporary directory (`ci-vm-proof-*`). Retain that value. Readiness means exit 0 and `evidence/launch.json` has `ready: true`. It snapshots the current installer/application bytes, installs three synthetic stopped-VM profiles into `scratch/isolated home`, and checks installer output in separate PTYs. No server remains running.

Each run owns a new home, tools, source snapshot, and temp directory, so separate runs can coexist. Do not drive the same run concurrently. Do not change the helper or its snapshot midway through a run. Launch does not use the real user's profile or shell startup files.

Teardown is `"$CI_VM_VERIFY_HELPER" cleanup "$CI_VM_VERIFY_RUN"`; retain the run directory for evidence. Failed launches print the directory before failing and clean their scratch state automatically.

## Doctor

```sh
"$CI_VM_VERIFY_HELPER" doctor "$CI_VM_VERIFY_RUN"
```

This checks source hashes, installed bytes, fake executable resolution/integrity, and the real installed `ci-vm --help`. It verifies help changes no home files and makes no boundary calls. It writes `evidence/doctor.json`; require `ready: true`. It only reads application state, while writing its own proof artifacts.

Run this first when anything looks off. This is the harness doctor, not `ci-vm doctor`: synthetic VMs are deliberately stopped, so their guest health cannot pass. A successful application doctor on a live VM also cannot prove network isolation or absence of compromise.

## Drive

```sh
"$CI_VM_VERIFY_HELPER" drive "$CI_VM_VERIFY_RUN" --feature profiles
```

Read [repository profiles](features/profiles.md). This drives the installed launcher in fresh PTYs: overview, inventory, setup guides, explicit selectors before/after the command, legacy selection, ambiguous/missing/repeated/abbreviated selector refusals. It checks terminal output and exit codes, profile bindings and permissions, the exact external call ledger, and before/after home hashes and mtimes.

For explicit sharing, use a separate fresh run:

```sh
"$CI_VM_VERIFY_HELPER" run --feature sharing
```

The phased equivalent is `drive "$CI_VM_VERIFY_RUN" --feature sharing` after launch and doctor. This changes the fake boundary to report VM `one` as Running and paused/idle, then uses the real installer to attach `another/three` to `verify/one`. It checks implicit-reuse refusal, exact rerun preservation, the member guide, and shared pause scope. Synthetic unit inventory simulates completed preparation; the guest preparation helper is not executed by this drive. See the profile map for supplementary helper tests and live proof gaps. Do not run the default profile drive after changing that run's fixture state.

The remaining feature recipes use the existing tests through the same isolated source snapshot:

```sh
"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature onboarding
"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature profiles
"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature sharing
"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature maintenance
"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature packages
"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature actions
```

These are supplementary regression groups with mixed subprocess and in-process mocks. The outer test process has a PTY; its internally captured commands do not. Do not call a whole feature verified solely because its regression group passes. Read each map's evidence gaps. The helper intentionally exposes no arbitrary live-command forwarding.

## Evidence

Evidence remains at **`$CI_VM_VERIFY_RUN/evidence/`** after cleanup:

- `source.json`: hashes of actual tested bytes, including uncommitted changes, Python version, and helper hash. A HEAD SHA alone would not identify this checkout.
- `launch-*.terminal.txt`, `drive-*.terminal.txt`, `sharing-*.terminal.txt`, and matching JSON: exact argv, merged PTY stdout/stderr, expected/observed exit codes, timeout status. Raw ANSI escapes preserve color; inspect the transcript, not just a passing summary.
- `boundary.jsonl`: synthetic external command arguments and Lima homes. In the default profile drive, local-only commands must add nothing and status/adoption may only call `limactl list --json` inside the temporary home. The sharing drive also permits its fake `limactl shell one` responses. Neither drive permits VM start, curl, or GitHub requests through these boundaries.
- `home-before.json` / `home-after.json`: hashes, modes, and mtimes proving inspection did not mutate installed files, profiles, or shell files.
- `drive.json`: asserted entry points and limitations; `checks-*.terminal.txt`: supplementary tests, including failures and skips.
- `drive-sharing.json`: sharing fixture identities, rerun and scope assertions, and explicit fake-guest limitations. Guest inventory responses are simulated, not live service evidence.
- `cleanup.json`: scratch removal and hashes of retained evidence. `failure.json`, when present, means the failed phase needs investigation, not success reporting.

Report actions and observed effects together. A guide is not executed setup; an online runner is not a completed job. Boundary mocks prove local command behavior only. They do not prove download availability, VM provisioning, package provenance, authentication, isolation, or successful Actions execution.

This harness is for reviewed source, not a security sandbox for malicious code: it strips inherited credentials and shadows known external tools, but does not enforce OS-level filesystem/network restrictions. Its call ledger observes these subprocess boundaries, not arbitrary sockets. Never claim a global no-network guarantee from it.

Keep proof outside the public checkout. Synthetic transcripts are safe to inspect locally; never capture registration tokens or copy live guest logs/credentials into evidence. Real setup/maintenance requires a separately authorized exact target, following `docs/llm-setup.md` and `docs/maintenance.md`. Real job evidence requires the exact run, commit, event, job names, and runner identity; never dispatch just to make this skill pass.

## Cleanup

```sh
"$CI_VM_VERIFY_HELPER" cleanup "$CI_VM_VERIFY_RUN"
test ! -e "$CI_VM_VERIFY_RUN/scratch"
test -s "$CI_VM_VERIFY_RUN/evidence/cleanup.json"
test -s "$CI_VM_VERIFY_RUN/evidence/drive.json"
```

The final check applies when the profile drive completed; for sharing check `evidence/drive-sharing.json` instead. Every command has a deadline and cleans only its own process group. Phase failures automatically remove owned scratch state and preserve evidence; launch a fresh run after failure. For manual interruptions, run cleanup explicitly too. Cleanup is idempotent, checks the run ownership marker, refuses symlink roots/scratch, and deletes only the fixed `scratch` child. Never replace it with `rm -rf` on a caller-supplied path, delete VMs, or kill by process name. The OS may eventually purge temporary evidence; copy the evidence directory to a user-approved private location if longer retention is needed.

## Helpers

The executable [scripts/verify_cli.py](scripts/verify_cli.py) supports `launch`, `doctor`, `drive`, `checks`, `cleanup`, and `run`; `--help` lists arguments. The `run` shortcut performs launch → doctor → profile drive → cleanup and prints the retained evidence directory's parent:

```sh
.agents/skills/verify-github-runner-vm/scripts/verify_cli.py run
```

For later code changes, use `$maintain-verification-skill` to update this feature map and rerun the affected paths. New commands or entry points require map updates; do not silently substitute one entry point for another.
