# ci-vm verification map

This is the maintained index of user-facing verification paths. Start with the parent skill's launch and doctor. Every command shown below belongs to the disposable fixture unless a separately approved live target is explicitly established. Never run fixture commands against your normal HOME.

## Features

| Feature | Entry points | Current proof scope |
| --- | --- | --- |
| [Onboarding](onboarding.md) | installer, bootstrap, shell PATH, overview/help, setup guide | Real installed overview/setup in profile drive; additional shell/bootstrap regressions |
| [Repository profiles](profiles.md) | inventory, exact/legacy selectors, explicit VM sharing, ambiguous or invalid selection | Installed CLI profile/sharing PTY drives; filesystem and boundary observations |
| [Runner maintenance](maintenance.md) | status, doctor, logs, pause, resume, restart | Synthetic status path and selected regression checks; live guest operations unverified |
| [Dependency packages](packages.md) | preview, interactive apply, unattended apply | In-process safety regressions; guest APT and interactive prompt need separate proof |
| [Actions verification](actions.md) | expected-run JSON/text receipts | In-process API-response regressions; real API/run evidence needs separate proof |

## Baseline and conventions

Launch creates `verify/one` → VM `one`, `verify/two` → VM `two`, and unassigned legacy VM `legacy`. All are fake stopped VMs in a unique temporary home. No runner credentials or IDs are read from the user's machine. No ports or daemon sessions are shared.

Run the helper from the repository root and retain `CI_VM_VERIFY_HELPER` and `CI_VM_VERIFY_RUN` from the skill's Launch section. Read a feature's preconditions before driving it. `drive --feature profiles` is the actual installed-CLI path. `checks --feature ...` invokes existing tests, not a substitute for missing live evidence.

Use a separate fresh run for `drive --feature sharing`. It reports synthetic VM `one` as Running and paused/idle, then attaches `another/three` to `verify/one` through the real installer. Its simulated member inventory is not proof that the guest preparation helper ran. Do not reuse this changed fixture for the stopped-profile drive.

## Proof and skip reporting

Pair exact command, output, exit status, and observable state. Read transcripts for skips. Report each untested entry point, unmet precondition, mock boundary, and inconclusive check. Do not report package installation, registration, or a successful Actions job from passing local tests. Retain evidence after cleanup. All feature files use the same four sections; update the map whenever entry points change.
