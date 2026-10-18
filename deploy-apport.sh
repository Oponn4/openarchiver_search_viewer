#!/bin/bash
# Apport-Backend neu bauen und ausrollen. Quelle: git archive eines Tags nach /opt/apport/src,
# Version steht in /opt/apport/VERSION (z.B. apport-2026-10-18). Altes Image bleibt für Rollback:
#   sed -i "s|^Image=.*|Image=localhost/apport:<alte-version>|" /etc/containers/systemd/apport.container
#   systemctl daemon-reload && systemctl restart apport
set -euo pipefail
cd /opt/apport
VERSION=$(cat VERSION)
# --network=host: ohne kommt apt/pip im Build nicht ins Netz (LXC ohne /dev/net/tun)
podman build --network=host -q -t "localhost/apport:${VERSION}" -f Containerfile .
sed -i "s|^Image=.*|Image=localhost/apport:${VERSION}|" /etc/containers/systemd/apport.container
systemctl daemon-reload
systemctl restart apport
sleep 4
systemctl is-active apport
