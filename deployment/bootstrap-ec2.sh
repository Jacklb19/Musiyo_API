#!/usr/bin/env bash
# Host preparation only: no database migration, cultural import or secret values.
set -euo pipefail

dnf install -y docker ca-certificates
# Amazon Linux 2023 supplies curl-minimal; installing curl conflicts with it.
command -v curl > /dev/null
systemctl enable --now docker
install -d -m 0750 /opt/musiyo /opt/musiyo/release /opt/musiyo/backups
install -d -m 0700 -o 10001 -g 10001 /opt/musiyo/config /opt/musiyo/secrets
install -d -m 0755 /usr/local/lib/docker/cli-plugins

compose_version=v2.40.3
compose_sha256=dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372
compose_file=$(mktemp)
trap 'rm -f "$compose_file"' EXIT
curl --fail --silent --show-error --location --retry 3 \
  "https://github.com/docker/compose/releases/download/${compose_version}/docker-compose-linux-x86_64" \
  --output "$compose_file"
printf '%s  %s\n' "$compose_sha256" "$compose_file" | sha256sum --check --status
install -m 0755 "$compose_file" /usr/local/lib/docker/cli-plugins/docker-compose
test "$(docker compose version --short)" = "${compose_version#v}"

cat > /etc/systemd/system/musiyo.service <<'SERVICE'
[Unit]
Description=Musiyo museum containers
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
ConditionPathExists=/opt/musiyo/config/runtime.env
ConditionPathExists=/opt/musiyo/release/compose.demo.yml

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/musiyo/release
ExecStartPre=/usr/bin/docker compose --env-file /opt/musiyo/config/runtime.env -f compose.demo.yml config --quiet
ExecStart=/usr/bin/docker compose --env-file /opt/musiyo/config/runtime.env -f compose.demo.yml up -d --no-build
ExecStop=/usr/bin/docker compose --env-file /opt/musiyo/config/runtime.env -f compose.demo.yml stop
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
# Enable recovery after a reboot, but do not start an unconfigured application.
systemctl enable musiyo.service
