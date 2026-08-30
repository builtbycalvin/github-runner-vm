# Verify an exact GitHub Actions run

Users prove that specified jobs completed successfully on the intended runner for an exact commit and event, rather than mistaking an online runner or dispatched run for working CI.

## Sub-features

- `expectations`: exact repository, run ID, full SHA, event, persisted runner ID, and the complete list of job names allowed on that runner.
- `attempt`: verify the correct attempt and complete paginated jobs.
- `receipt`: human output or JSON with verified/unverified status.
- `refusal`: reject mismatches, missing/failed jobs, incomplete pages, or concurrent reruns.

## How to get to it (user POV)

- `ci-vm verify-run --help` lists the required expectations.
- Selected-profile `verify-run` with `--expect-sha`, `--expect-event`, `--expect-runner-id`, and repeated `--job` for every required job.
- Add `--json` for a structured receipt; omit it for terminal presentation. Unassigned legacy configurations cannot supply repository identity.

## Driving it with verify_cli.py

Preconditions: default fixture has no GitHub authentication or real run identity. Do not invent an ID, select an arbitrary latest run, or dispatch a workflow for this local proof.

- **Assessment regressions.** Run `"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature actions`. Read `checks-actions.terminal.txt` for exact attempt/pagination behavior, successful required jobs with other hosted jobs allowed, persisted-profile identity refusal before API access, unlisted selected-runner jobs, identity/missing/failed-job refusals, and concurrent rerun detection.
- **Proof boundary.** This group uses mocked API responses and is not an authenticated request or a real job result. It does not prove both receipt presentations through the installed launcher.
- **Authorized real readback.** Obtain expectations from the user's approved exact workflow operation and recorded run ID, following `docs/llm-setup.md`. Run the installed selected-profile command with those expectations and observe exit status, the JSON receipt, and readback identity. An existing run may be inspected without dispatching another. Do not capture auth tokens, private logs, or runner credentials; keep any private run details outside this public repo.

## Gotchas

- A smoke job proves connectivity, not the target repository's dependency readiness.
- A run's overall conclusion cannot substitute for checking every required job and runner ID. An unlisted job on the selected runner makes the result unverified.
- A current rerun can invalidate earlier observations. Incomplete or conflicting results remain unverified.
- No VM or workflow should be changed by run verification; inspect external commands and report that boundary separately from result correctness.
