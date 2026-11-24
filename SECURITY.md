# Security policy

This dashboard can start, stop and restart Docker containers, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private reporting instead: go to the **Security** tab of this repository and choose **Report a vulnerability** (private vulnerability reporting). Include what you found, how to reproduce it, and which version or commit you tested.

This is a hobby project maintained in spare time: expect an acknowledgement within about a week, and a fix or a clear answer as soon as is practical. Please give me reasonable time to fix an issue before disclosing it publicly.

## Scope and supported versions

Only the latest commit on `main` is supported. This project is designed to run **only on a trusted LAN and/or a private VPN such as Tailscale** and must never be exposed to the internet (see the README). Reports that depend on exposing it to the internet or on an attacker who already has root on the host are out of scope, but hardening suggestions are welcome as normal issues.

## If you ran it with a leaked password hash or secret

Run `./set-password.sh` to set a new password, regenerate the TLS certificate with `./make-cert.sh`, and restart the container. Sessions are held in memory only, so restarting also logs everyone out.
