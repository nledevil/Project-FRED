# Retired deploy pieces

Kept for reference, moved out of the active `deploy/` tree so nothing here is one
`systemctl enable` away from breaking the live robot.

## `pan/` — the Bluetooth PAN between the head and chest (retired 2026-09-09)

This was the head↔chest link back when the **head was the brain** and held
`10.0.0.1`. The brain is the NUC now; it serves the robot LAN over wired
ethernet (`10.0.0.1`), and both Pis are static clients (head `10.0.0.10`, chest
`10.0.0.11` — see `deploy/net-head/`, `deploy/net-chest/`). Enabling
`pan-server.service` today would put `10.0.0.1` on the head and collide with the
NUC — which is exactly why it no longer lives under `deploy/`. The services are
`disabled` on the machines; this directory is the historical record only.
