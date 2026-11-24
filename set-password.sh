#!/bin/bash
# Set (or change) the dashboard password. Writes only a scrypt hash to .env (mode 600, never committed).
# There is no default password: the dashboard refuses to start until this has been run.
# Usage: ./set-password.sh        (prompts twice)
#        DASH_PW=... ./set-password.sh   (non-interactive; the password is then visible in your shell history/env)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
if [ -z "${DASH_PW:-}" ]; then
  read -rsp "New dashboard password (min 12 chars): " DASH_PW; echo
  read -rsp "Repeat it: " pw2; echo
  [ "$DASH_PW" = "$pw2" ] || { echo "passwords differ"; exit 1; }
fi
export DASH_PW
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && echo "created .env from .env.example: edit the addresses in it before starting"; }
python3 -I - <<'PY'
import base64, hashlib, os, re, sys
pw = os.environ["DASH_PW"]
if len(pw) < 12: sys.exit("password too short (min 12)")
salt = os.urandom(16)
h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, maxmem=128*1024*1024, dklen=32)
b = lambda x: base64.urlsafe_b64encode(x).decode()
line = f"DASH_PASSWORD_HASH=scrypt:16384:8:1:{b(salt)}:{b(h)}"
try: old = open(".env").read()
except FileNotFoundError: old = ""
old = re.sub(r"^DASH_PASSWORD_HASH=.*\n?", "", old, flags=re.M)
if old and not old.endswith("\n"): old += "\n"
fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, "w") as f: f.write(old + line + "\n")
print("password hash written to .env")
PY
echo "Apply with: docker compose up -d --force-recreate dashboard"
