#!/bin/bash
# Self-signed TLS cert for the LAN and Tailscale addresses (your browser warns once).
# Reads LAN_IP and TAILSCALE_IP from .env, or pass them:  ./make-cert.sh 192.0.2.10 100.64.0.10
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
envval() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//' || true; }
lan="${1:-$(envval LAN_IP)}"; ts="${2:-$(envval TAILSCALE_IP)}"
[ -n "$lan" ] || { echo "usage: $0 <lan-ip> [tailscale-ip]   (or set LAN_IP in .env)"; exit 1; }
san="IP:$lan"; [ -z "$ts" ] || san="$san,IP:$ts"
mkdir -p certs && chmod 700 certs
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 \
  -keyout certs/key.pem -out certs/cert.pem -subj "/CN=homelab-dashboard" -addext "subjectAltName=$san"
chmod 600 certs/key.pem; chmod 644 certs/cert.pem
echo "certificate written to certs/ for: $san"
echo "The container runs as PUID:PGID from .env; certs/key.pem must be readable by that user."
