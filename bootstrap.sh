#!/bin/sh

bootstrap() {
    set -eu
    if [ "$#" -eq 0 ]; then
        echo 'Pass --adopt NAME or --provision NAME --yes-create-vm. Add --configure-shell for PATH setup.' >&2
        return 2
    fi
    for tool in curl tar bash python3 limactl; do
        command -v "$tool" >/dev/null 2>&1 || { echo "Install prerequisite: $tool" >&2; return 2; }
    done
    revision=${CI_VM_REF:-main}
    if [ "$revision" != main ]; then
        case "$revision" in *[!0-9a-fA-F]*) echo 'CI_VM_REF must be main or a full 40-character commit SHA.' >&2; return 2;; esac
        [ "${#revision}" -eq 40 ] || { echo 'CI_VM_REF must be main or a full 40-character commit SHA.' >&2; return 2; }
    fi
    bootstrap_tmp=$(mktemp -d "${TMPDIR:-/tmp}/github-runner-vm.XXXXXXXX")
    trap 'rm -rf -- "$bootstrap_tmp"' 0
    trap 'exit 130' INT
    trap 'exit 143' HUP TERM
    echo "Downloading github-runner-vm source at $revision"
    curl -fsSL --proto '=https' --connect-timeout 10 --max-time 120 \
        "https://github.com/builtbycalvin/github-runner-vm/archive/$revision.tar.gz" \
        -o "$bootstrap_tmp/source.tar.gz"
    mkdir "$bootstrap_tmp/source"
    tar -xzf "$bootstrap_tmp/source.tar.gz" -C "$bootstrap_tmp/source" --strip-components=1
    bash "$bootstrap_tmp/source/install.sh" "$@" </dev/null
}

bootstrap "$@"
