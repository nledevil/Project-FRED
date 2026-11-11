# The NUC's load-bearing network config

The NUC is the single machine whose loss kills everything: it bridges both
wired NICs into `br0` (it *is* the switch since the real one died 2026-08-09),
owns 10.0.0.1, and serves the robot LAN's DHCP, DNS and NTP. Until 2026-09-09
these files existed only on its disk — a dead drive meant reconstructing the
network from memory at a venue.

| repo copy | installed at |
|---|---|
| `00-installer-config.yaml` | `/etc/netplan/00-installer-config.yaml` |
| `fred-dnsmasq.conf`        | `/etc/dnsmasq.d/fred.conf` |
| `robot-lan-chrony.conf`    | `/etc/chrony/conf.d/robot-lan.conf` |

`deploy/check-host-config.sh` diffs all three against the machine, the same
way it already watches the head's `interfaces` and the NAT files. Two NUC
files are deliberately NOT tracked here: `/etc/netplan/60-fred-uplink.yaml`
is rewritten by `fred-uplink` whenever somebody joins a network (live state,
not config — the backup captures it), and the hostapd config carries the AP
password (secrets ride in `tools/backup.sh`'s archive, never the repo).

Rebuild recipe: SERVICE.md, "Rebuilding the brain".
