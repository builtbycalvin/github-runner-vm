#!/bin/bash
set -euo pipefail

state=/var/lib/ci-vm
if [ -f "$state/provision-version" ]; then
    test "$(cat "$state/provision-version")" = 1
    exit
fi
test "$(uname -m)" = aarch64
. /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04
install -d -m 755 "$state"
touch "$state/paused"
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update
apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y \
    ca-certificates curl gnupg uidmap dbus-user-session slirp4netns \
    fuse-overlayfs nftables apparmor apparmor-utils jq git unzip \
    libicu74 libssl3t64 zlib1g libkrb5-3 unattended-upgrades

if ! id ci >/dev/null 2>&1; then
    if getent passwd 1001; then
        echo 'UID 1001 is already used. Refusing to change another account.' >&2
        exit 1
    fi
    if getent group ci >/dev/null; then
        test "$(getent group ci | cut -d: -f3)" = 1001
    else
        if getent group 1001; then
            echo 'GID 1001 is already used. Refusing to change another group.' >&2
            exit 1
        fi
        groupadd --gid 1001 ci
    fi
    useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash ci
fi
test "$(id -u ci):$(id -g ci):$(id -Gn ci)" = 1001:1001:ci
if runuser -u ci -- sudo -n true 2>/dev/null; then
    echo 'The CI account has sudo access. Refusing to continue.' >&2
    exit 1
fi
sed -i '/^ci:/d' /etc/subuid /etc/subgid
printf 'ci:200000:65536\n' >> /etc/subuid
printf 'ci:200000:65536\n' >> /etc/subgid
chmod 700 /home/ci /home/limaadmin
install -d -o ci -g ci -m 700 /home/ci/work /home/ci/actions-runner

cat > /etc/nftables.conf <<'NFT'
#!/usr/sbin/nft -f
table inet ci_vm
flush table inet ci_vm
table inet ci_vm {
    chain output {
        type filter hook output priority 0; policy accept;
        meta skuid { 1001, 200000-265535 } jump ci_egress
    }
    chain ci_egress {
        oifname "lo" accept
        ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4 } reject
        meta nfproto ipv6 reject
    }
}
NFT
nft -c -f /etc/nftables.conf
nft -f /etc/nftables.conf
systemctl enable --now nftables.service
install -d -m 755 /etc/systemd/system/user@1001.service.d
cat > /etc/systemd/system/user@1001.service.d/ci-vm.conf <<'SYSTEMD'
[Unit]
Requires=nftables.service
After=nftables.service
SYSTEMD
systemctl daemon-reload

systemctl mask docker.service docker.socket containerd.service
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --location --max-time 60 \
    https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
fingerprint=$(gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc | awk -F: '$1 == "fpr" { print $10; exit }')
test "$fingerprint" = 9DC858229FC7DD38854AE2D88D81803C0EBFCD88
chmod 644 /etc/apt/keyrings/docker.asc
printf '%s\n' 'deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable' > /etc/apt/sources.list.d/docker.list
apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update
apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y \
    docker-ce=5:29.7.2-1~ubuntu.24.04~noble \
    docker-ce-cli=5:29.7.2-1~ubuntu.24.04~noble \
    docker-ce-rootless-extras=5:29.7.2-1~ubuntu.24.04~noble \
    containerd.io=2.3.3-1~ubuntu.24.04~noble \
    docker-buildx-plugin=0.36.1-1~ubuntu.24.04~noble \
    docker-compose-plugin=5.5.0-1~ubuntu.24.04~noble
apt-mark hold docker-ce docker-ce-cli docker-ce-rootless-extras containerd.io docker-buildx-plugin docker-compose-plugin
test "$(cat /sys/module/apparmor/parameters/enabled)" = Y
test "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns)" = 1
if [ ! -f /etc/apparmor.d/usr.bin.rootlesskit ]; then
cat > /etc/apparmor.d/usr.bin.rootlesskit <<'APPARMOR'
abi <abi/4.0>,
include <tunables/global>
/usr/bin/rootlesskit flags=(unconfined) {
    userns,
    include if exists <local/usr.bin.rootlesskit>
}
APPARMOR
fi
apparmor_parser -r /etc/apparmor.d/usr.bin.rootlesskit
loginctl enable-linger ci
systemctl start user@1001.service
runuser -u ci -- env HOME=/home/ci XDG_RUNTIME_DIR=/run/user/1001 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus \
    dockerd-rootless-setuptool.sh install
runuser -u ci -- env XDG_RUNTIME_DIR=/run/user/1001 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus \
    systemctl --user enable --now docker.service
cat > /home/ci/.profile <<'PROFILE'
export XDG_RUNTIME_DIR=/run/user/1001
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus
export DOCKER_HOST=unix:///run/user/1001/docker.sock
export PATH="$HOME/.local/bin:$PATH"
PROFILE
chown ci:ci /home/ci/.profile
cat > /etc/apt/apt.conf.d/51ci-vm <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "0";
Unattended-Upgrade::Automatic-Reboot "false";
APT
printf '1\n' > "$state/provision-version"
