#!/bin/bash

ci_vm_container_runtime_state() {
    local user=$1 uid=$2
    sudo -n bash -s -- "$user" "$uid" <<'CI_VM_RUNTIME'
set -euo pipefail
user=$1
uid=$2
expected_socket="/run/user/$uid/docker.sock"
expected_fragment="/home/$user/.config/systemd/user/docker.service"
runtime_base=/run
runtime_drift=no

user_ctl() {
    runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user "$@"
}
docker_ci() {
    runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" DOCKER_HOST="unix://$expected_socket" docker "$@"
}

test "$(user_ctl show docker.service --property=LoadState --value)" = loaded || runtime_drift=yes
test "$(user_ctl show docker.service --property=ActiveState --value)" = active || runtime_drift=yes
test "$(user_ctl show docker.service --property=SubState --value)" = running || runtime_drift=yes
fragment=$(user_ctl show docker.service --property=FragmentPath --value)
fragment=$(readlink -f -- "$fragment")
test "$fragment" = "$expected_fragment" || runtime_drift=yes
test "$(stat -c '%u:%g:%a' "$fragment")" = "$uid:$uid:644" || runtime_drift=yes
docker_cgroup=$(user_ctl show docker.service --property=ControlGroup --value)
case "$docker_cgroup" in /user.slice/*/docker.service) ;; *) runtime_drift=yes;; esac

for unit in docker.service docker.socket containerd.service; do
    test "$(systemctl show "$unit" --property=LoadState --value)" = masked || runtime_drift=yes
    test "$(systemctl show "$unit" --property=UnitFileState --value)" = masked || runtime_drift=yes
    test "$(systemctl show "$unit" --property=ActiveState --value)" = inactive || runtime_drift=yes
done

runtime_roots=("$runtime_base")
for candidate in "$runtime_base"/user/*; do
    if test -d "$candidate" && test ! -L "$candidate"; then runtime_roots+=("$candidate"); fi
done
sockets=$(find "${runtime_roots[@]}" -xdev -type s \( -name docker.sock -o -name podman.sock -o -name containerd.sock -o -name crio.sock \) -print | LC_ALL=C sort | uniq)
test "$sockets" = "$expected_socket" || runtime_drift=yes
test "$(stat -c '%u:%g' "$expected_socket")" = "$uid:$uid" || runtime_drift=yes

if ! security=$(docker_ci info --format '{{json .SecurityOptions}}'); then exit 8; fi
case "$security" in *rootless*) ;; *) runtime_drift=yes;; esac
if ! container_ids=$(docker_ci ps -q); then exit 8; fi
if test -n "$container_ids"; then containers=$(printf '%s\n' "$container_ids" | awk 'END {print NR+0}'); else containers=0; fi

dockerd=0
containerd=0
while read -r pid process_uid command; do
    test -n "${pid:-}" || continue
    case "$command" in
        podman|conmon|crio|lxc-start|lxc-monitor|incusd|lxd|systemd-nspawn) runtime_drift=yes; continue;;
        dockerd) dockerd=$((dockerd + 1));;
        containerd) containerd=$((containerd + 1));;
        rootlesskit|slirp4netns|containerd-shim*|docker-proxy|runc) ;;
        *) continue;;
    esac
    test "$process_uid" = "$uid" || runtime_drift=yes
    process_cgroup=$(awk -F: '$1 == "0" {print $3}' "/proc/$pid/cgroup")
    case "$process_cgroup" in "$docker_cgroup"|"$docker_cgroup"/*) ;; *) runtime_drift=yes;; esac
done < <(ps -eo pid=,uid=,comm=)
test "$dockerd" = 1 || runtime_drift=yes
test "$containerd" = 1 || runtime_drift=yes

printf 'Containers=%s\nRuntimeDrift=%s\n' "$containers" "$runtime_drift"
CI_VM_RUNTIME
}
