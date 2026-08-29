#!/bin/bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 3; }
test "$(id -u)" = 0 || fail 'Run this reviewed helper as the guest administrator.'
test "$#" = 2 || { test "$#" = 3 && test "$1" = finish && test "$3" = --registration-ready; } || fail 'Usage: prepare-shared-runner.sh prepare KEY | finish KEY --registration-ready'
action=$1
key=$2
[[ "$key" =~ ^[a-z0-9][a-z0-9-]{0,35}-[a-f0-9]{12}$ ]] || fail 'Invalid repository key.'
case "$action" in prepare) test "$#" = 2;; finish) test "$#" = 3;; *) fail 'Unknown operation.';; esac
state=/var/lib/ci-vm
unit_dir=/etc/systemd/user
runner_parent=/home/ci/runners
work_parent=/home/ci/work
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
template="$script_dir/ci-vm-runner@.service"
unit="ci-vm-runner@$key.service"
test -f "$template" && test ! -L "$template" || fail 'Missing reviewed service template.'
test "$(id -u ci):$(id -g ci):$(id -Gn ci)" = 1001:1001:ci || fail 'CI account differs.'
test ! -L "$state" && test "$(stat -c '%u:%a' "$state")" = 0:755 || fail 'State directory differs.'
test ! -L "$state/operation.lock" || fail 'Lock path is a symlink.'
exec 9>"$state/operation.lock"
flock -n 9 || fail 'Another guest operation is still running.'
test -f "$state/paused" && test ! -L "$state/paused" && test "$(stat -c '%u' "$state/paused")" = 0 || fail 'Pause the complete VM before preparation.'
test ! -e "$state/package-maintenance" && test ! -L "$state/package-maintenance" || fail 'Package maintenance is unfinished.'
test ! -L "$unit_dir" && test "$(stat -c '%u:%a' "$unit_dir")" = 0:755 || fail 'Unit directory differs.'

ctl() { runuser -u ci -- env XDG_RUNTIME_DIR=/run/user/1001 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus systemctl --user "$@"; }
render() { sed "s/@KEY@/$1/g" "$template"; }
expected() {
    if test "$1" = ci-vm-runner.service; then
        sed 's,/home/ci/runners/@KEY@,/home/ci/actions-runner,g' "$template"
    else
        local member=${1#ci-vm-runner@}
        member=${member%.service}
        [[ "$member" =~ ^[a-z0-9][a-z0-9-]{0,35}-[a-f0-9]{12}$ ]] || fail 'Unknown managed unit name.'
        render "$member"
    fi
}
check_unit() {
    local member=$1 fragment cg pending=no files file value
    test -f "$unit_dir/$member" && test ! -L "$unit_dir/$member" || fail 'Missing or linked member unit.'
    test "$(stat -c '%u:%a' "$unit_dir/$member")" = 0:644 || fail 'Member unit ownership differs.'
    cmp -s "$unit_dir/$member" <(expected "$member") || fail 'Member unit bytes differ.'
    if test "$action" = prepare && test "$member" = "$unit" && test "$recovering" = yes; then pending=yes; fi
    fragment=$(ctl show "$member" --property=FragmentPath --value)
    if test "$pending" != yes || test -n "$fragment"; then test "$(readlink -f -- "$fragment")" = "$unit_dir/$member" || fail 'Member service override differs.'; fi
    value=$(ctl show "$member" --property=LoadState --value)
    case "$value" in loaded) ;; not-found) test "$pending" = yes || fail 'Member service is not loaded.';; *) fail 'Unknown load state.';; esac
    value=$(ctl show "$member" --property=DropInPaths --value)
    test -z "$value" || fail 'Member service has drop-ins.'
    value=$(ctl show "$member" --property=NeedDaemonReload --value)
    test "$value" = no || test "$pending" = yes || fail 'Member service needs reload.'
    value=$(ctl show "$member" --property=Transient --value)
    test "$value" = no || fail 'Transient member service refused.'
    value=$(ctl show "$member" --property=ActiveState --value)
    test "$value" = inactive || fail 'Member service is not inactive.'
    value=$(ctl show "$member" --property=SubState --value)
    test "$value" = dead || fail 'Member service is not dead.'
    value=$(ctl show "$member" --property=MainPID --value)
    test "$value" = 0 || fail 'Member service has a process.'
    value=$(ctl show "$member" --property=ControlPID --value)
    test "$value" = 0 || fail 'Member service has a control process.'
    value=$(ctl show "$member" --property=Job --value)
    test -z "$value" || fail 'Member service has a pending job.'
    cg=$(ctl show "$member" --property=ControlGroup --value)
    if test -n "$cg"; then
        case "$cg" in /user.slice/*) ;; *) fail 'Unknown member cgroup.';; esac
        if test -d "/sys/fs/cgroup$cg"; then
            files=$(find "/sys/fs/cgroup$cg" -name cgroup.procs -type f)
            test -n "$files" || fail 'Member cgroup evidence is missing.'
            while IFS= read -r file; do value=$(cat "$file"); test -z "$value" || fail 'Member cgroup is not empty.'; done <<< "$files"
        fi
    fi
}
check_idle() {
    local member inventory containers jobs loaded enabled processes
    processes=$(ps -eo comm= | awk '$1 == "Runner.Listener" || $1 == "Runner.Worker" {n++} END {print n+0}')
    test "$processes" = 0 || fail 'Runner processes remain.'
    containers=$(runuser -u ci -- env XDG_RUNTIME_DIR=/run/user/1001 DOCKER_HOST=unix:///run/user/1001/docker.sock docker ps -q)
    test -z "$containers" || fail 'Containers remain.'
    jobs=$(ctl list-jobs --no-legend --no-pager)
    test -z "$jobs" || fail 'Service jobs remain.'
    for member in "$unit_dir"/ci-vm-runner*.service; do check_unit "${member##*/}"; done
    loaded=$(ctl list-units --all --plain --no-legend --no-pager 'ci-vm-runner*')
    enabled=$(ctl list-unit-files --no-legend --no-pager 'ci-vm-runner*')
    inventory=$(printf '%s\n%s\n' "$loaded" "$enabled" | awk 'NF {print $1}' | LC_ALL=C sort -u)
    while IFS= read -r member; do
        test -n "$member" || continue
        test -f "$unit_dir/$member" && test ! -L "$unit_dir/$member" || fail 'Loaded or enabled unit is missing from the persistent roster.'
    done <<< "$inventory"
}
directory() {
    local path=$1
    test ! -L "$path" || fail 'Runner directory is a symlink.'
    if test -e "$path"; then
        test -d "$path" && test "$(stat -c '%u:%g:%a' "$path")" = 1001:1001:700 || fail 'Runner directory ownership differs.'
    else
        test "$action" = prepare || fail 'Prepared runner directory is missing.'
        install -d -o ci -g ci -m 700 "$path"
    fi
}

recovering=no
finished=no
if test -e "$state/shared-setup" || test -L "$state/shared-setup"; then
    test -f "$state/shared-setup" && test ! -L "$state/shared-setup" && test "$(stat -c '%u:%a' "$state/shared-setup")" = 0:600 || fail 'Shared setup gate differs.'
    test "$(cat "$state/shared-setup")" = "$key" || fail 'Another member setup is unfinished.'
    recovering=yes
fi
check_idle
if test "$recovering" = yes; then
    :
elif test "$action" = prepare; then
    if test ! -e "$unit_dir/$unit"; then
        for path in "$runner_parent/$key" "$work_parent/$key"; do
            test ! -e "$path" && test ! -L "$path" || fail 'Unexplained member directory exists. Preserve it and inspect before setup.'
        done
    fi
    temporary=$(mktemp "$state/.shared-setup.XXXXXX")
    trap 'rm -f -- "$temporary"' EXIT
    printf '%s\n' "$key" > "$temporary"
    chmod 600 "$temporary"
    ln -- "$temporary" "$state/shared-setup"
    rm -- "$temporary"
    trap - EXIT
else
    test -f "$unit_dir/$unit" && test "$(ctl is-enabled "$unit")" = enabled || fail 'No matching prepared member to finish.'
    finished=yes
fi
if test "$finished" = no; then
    test "$(stat -c '%u:%a' "$state/shared-setup")" = 0:600 && test "$(cat "$state/shared-setup")" = "$key" || fail 'Shared setup gate could not be confirmed.'
fi
directory "$runner_parent"
directory "$runner_parent/$key"
directory "$work_parent"
directory "$work_parent/$key"
if test "$action" = prepare; then
    if test -e "$unit_dir/$unit" || test -L "$unit_dir/$unit"; then
        check_unit "$unit"
    else
        temporary=$(mktemp "$unit_dir/.ci-vm-unit.XXXXXX")
        trap 'rm -f -- "$temporary"' EXIT
        render "$key" > "$temporary"
        chmod 644 "$temporary"
        ln -- "$temporary" "$unit_dir/$unit"
        rm -- "$temporary"
        trap - EXIT
    fi
    ctl daemon-reload
    recovering=no
    check_unit "$unit"
    check_idle
    printf '%s\n' "Prepared $unit. Shared setup gate remains set. Download the runner into /home/ci/runners/$key, then run ci-vm register for the selected repository profile. That transaction enables the inactive unit, finishes this gate, and leaves the VM paused."
else
    check_unit "$unit"
    test "$(ctl is-enabled "$unit")" = enabled || fail 'Enable the exact member unit without --now before finishing.'
    test -x "$runner_parent/$key/bin/Runner.Listener" || fail 'Runner executable is missing.'
    check_idle
    if test "$finished" = no; then rm -- "$state/shared-setup"; fi
    printf '%s\n' 'Shared setup finished. VM remains paused.'
fi
