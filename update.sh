#!/bin/bash
# The locally built dashboard image is not updated by Watchtower. Run this occasionally (e.g. monthly).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
docker compose build --pull dashboard
docker compose up -d dashboard
