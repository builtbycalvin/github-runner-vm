# Inspect and maintain the selected runner

Users inspect health and logs, request cooperative pause, and resume or restart only when the selected VM satisfies the maintenance contract.

## Sub-features

- `inspect`: status, doctor, and bounded recent logs.
- `pause`: request pause, distinguish idle from pending, preserve a pending gate.
- `resume`: reject stopped/unsupported/unfinished-package states.
- `restart`: require already paused idle state and leave the runner paused.
- `shared`: require `--all-repos` for mutations, enumerate every member, and check every cgroup before claiming idle. Restore pause after a partial resume failure.

## How to get to it (user POV)

- `ci-vm --repo verify/one status`, `ci-vm --repo verify/one doctor`, `ci-vm --repo verify/one logs --lines 100`.
- `ci-vm --repo verify/one pause --timeout 30`.
- `ci-vm --repo verify/one resume`, `ci-vm --repo verify/one restart`.
- `--legacy` selects an existing legacy installation instead of `--repo`.
- Add `--all-repos` to pause, resume, or restart only after the user approves effects on every repository listed for a shared VM. Logs remain scoped to the selected runner.

## Driving it with verify_cli.py

Preconditions: the default fixture is stopped; do not make it a live VM to satisfy these recipes. Actual maintenance requires explicit user authorization and the exact identity checks in `docs/maintenance.md`.

- **Stopped inspection.** Run `"$CI_VM_VERIFY_HELPER" drive "$CI_VM_VERIFY_RUN" --feature profiles`. Each selected status returns 0 and `Stopped`; the call ledger shows no start or guest invocation.
- **Pause and guards.** Run `"$CI_VM_VERIFY_HELPER" checks "$CI_VM_VERIFY_RUN" --feature maintenance`. Existing subprocess tests exercise the installed pause command against fake running/idle and running/pending guest responses. Observe marker creation and exit 0 versus pending exit 4. Other tests check restart guards, stopped resume, preserved pause, exact GitHub identity readback, and sanitized failures.
- **Live proof gap.** The group does not prove a real service drained, any VM restarted, or live `doctor`/`logs` output. If separately authorized, follow the runbook on the exact selected VM, capture sanitized before/after identity and state, and report each entry point separately. Keep raw logs private. Do not dispatch a job or interrupt a listener to manufacture idleness.

## Gotchas

- GitHub `busy: false` does not close the assignment race. A pause may accept one last job.
- Exit 4 means pending/timeout, not idle. Do not clear gates or force restart.
- A shared setup gate blocks resume and restart until the exact member's reviewed preparation and automatic registration transaction complete. A missing, extra, or modified member unit is a failure, not permission to ignore that runner.
- `--all-repos` acknowledges scope. It does not grant authorization or make shared jobs run serially.
- Shared resume requires every member to be paused and idle. Repeating resume while a member is active refuses; first request pause and let the entire group drain.
- Application doctor may contain INCONCLUSIVE checks even on exit 0.
- The harness cleanup only removes synthetic state. It must never stop, delete, or replace a live VM.
