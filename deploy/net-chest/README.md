# Chest Pi network config

`interfaces` is a copy of `/etc/network/interfaces` on the chest Pi (the 7" DSI
panel Pi), tracked byte for byte so `deploy/check-host-config.sh` can tell you
when the machine and the repo have drifted apart. Same story as
`deploy/net-head/`: **installed by hand**, because `push-role.sh` deploys a tree
into one root per machine and nothing here owns `/etc` on the Pis.

```
scp deploy/net-chest/interfaces dietpi@10.0.0.11:/tmp/
ssh dietpi@10.0.0.11 'sudo cp /etc/network/interfaces /etc/network/interfaces.bak &&
                      sudo cp /tmp/interfaces /etc/network/interfaces'
```

Take the backup. Getting this wrong strands the Pi behind the chest panel.

## eth0 is static (R7a, 2026-09-09)

`eth0` is `inet static` at `10.0.0.11/24`, gateway `10.0.0.1` — the address the
NUC's DHCP reserved (MAC `dc:a6:32:c8:f6:15`), pinned so the Pi no longer waits
on the NUC's DHCP at boot. Unlike the head, the chest **does** carry the gateway:
its only default route was already `via 10.0.0.1 dev eth0` (the NUC masquerades
`10.0.0.0/24` to its uplink), so keeping it preserves the chest's internet path
exactly. The address does not change.

Converted live behind a boot self-heal (a one-shot systemd unit that restored
DHCP and rebooted if `10.0.0.1` was unreachable), which passed and was removed.
`/etc/network/interfaces.dhcp.bak` on the Pi is the pre-change DHCP config —
restore it and reboot to go back:

```
ssh dietpi@10.0.0.11 'sudo cp /etc/network/interfaces.dhcp.bak /etc/network/interfaces && sudo reboot'
```

## Why static, when a DHCP reservation already pinned the address

The reservation made the *address* stable, but the Pi still had to complete a
DHCP exchange with the NUC at boot to get it — a boot-order dependency on the NUC
being up and serving first. Static removes that: the panel Pi comes up on the
robot LAN whether or not the NUC's `dnsmasq` is ready, which is the point of
R7a ("survive the network being bad or the brain being gone").

## Not tracked, deliberately

`/etc/wpa_supplicant/wpa_supplicant.conf` holds WiFi passwords in the clear and
has no business in a git repository.
