# Install and find the next step

Users install ci-vm, find it in their shell, and receive a repository-specific setup guide that clearly separates VM installation from runner registration and job verification.

## Sub-features

- `install`: install from a reviewed checkout or downloaded source archive.
- `shell`: opt into persistent Bash/Zsh PATH configuration and preserve rerun behavior.
- `discover`: bare overview, `--help`, and command-specific help.
- `setup`: managed repository and legacy/adopted-service guides.
- `register`: use authenticated host `gh` for unattended, exact, retry-safe registration while keeping the token out of arguments and retained verification.

## How to get to it (user POV)

- `bash install.sh --adopt one --repo verify/one` (synthetic launch fixture).
- The documented pinned bootstrap pipeline, with explicit provision consent, or installer `--provision` and sizing flags.
- Installer `--configure-shell`, then `ci-vm` in a new Bash or Zsh session.
- `ci-vm`, `ci-vm --help`, `ci-vm setup --help`, `ci-vm setup verify/one`, and `ci-vm --legacy setup verify/legacy`.
- `ci-vm --repo OWNER/REPO register` after the runner download and paused-idle checks. If needed, approve `gh auth login --hostname github.com --web` in the browser first.

## Driving it with verify_cli.py

Preconditions: use a fresh run from the parent skill; no live install/provision authorization is implied.

- **Installed entry point.** Launch runs the real installer three times and verifies `Installed ci-vm.`. Doctor invokes the installed `--help`. Run `"$CI_VM_VERIFY_HELPER" drive "$CI_VM_VERIFY_RUN" --feature profiles` for the bare overview and both repository/legacy guide paths; inspect `drive-overview.terminal.txt` and `drive-setup*.terminal.txt`.
- **Shell, archive, and registration paths.** Run `"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature onboarding`. Read `checks-onboarding.terminal.txt` for installer rerun, opt-in/idempotent PATH changes, extracted-source independence, fake-download Bash/Zsh pipeline tests, bounded automatic VM names, pre-write Unix socket path refusal, stdin-only credential transport, registration convergence, inactive enablement, shared finish ordering, and the manual fallback's uncaptured terminal deadline. Missing Zsh coverage must be reported, not inferred from Bash.
- **Guide distinction.** The guide must name the intended VM/repository and say no commands were executed. The supplemental tests cover preserving an adopted service. Inspect the emitted registration, service, and exact-run next steps rather than treating a guide as completed setup.
- **Remaining paths.** Command-specific help, new-VM creation/sizing, a real download, and full agent cold onboarding need separate evidence. For those use the reviewed procedure in `docs/llm-setup.md`, explicit operational authorization, and a fresh isolated session. Do not execute the public bootstrap during local verification.

## Live registration proof

A separately authorized managed-profile run proved authenticated credential retrieval, stdin-only guest configuration, exact local and GitHub reconciliation, private registration files, runner ID persistence, inactive unit enablement, and a paused result. The synthetic checks remain the reproducible local evidence and do not prove a workflow dispatch or completed job.

## Gotchas

- A plain repository URL follows its default branch, not this checkout's uncommitted bytes. This skill proves the snapshot identified in `source.json` only.
- No subprocess can change the caller's PATH. The installed absolute launcher works immediately; configured shells need a new session.
- Synthetic registration may run in captured tests because the fake credential must never appear in retained argv, output, errors, profiles, or evidence.
- Unit tests prove boundary shape only. A separately authorized live test must prove the installed runner accepts `ACTIONS_RUNNER_INPUT_TOKEN` and that a retry does not mint another token.
- PTY transcripts preserve ANSI colors. No screenshot or interactive prompt coverage is implied by the text assertions.
