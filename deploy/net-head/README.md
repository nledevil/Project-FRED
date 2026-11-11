# Head Pi network config

`interfaces` is a copy of `/etc/network/interfaces` on the head Pi, tracked
byte for byte so `deploy/check-host-config.sh` can tell you when the machine and
the repo have drifted apart.

It is **installed by hand**, like `deploy/hotspot-nuc/fred-nat.nft` and for the
same reason: `push-role.sh` deploys a tree into one root per machine, and
nothing in this repo owns `/etc` on the Pis. Adding a second, absolute-path
deploy mechanism to cover two files is worse than copying them.

```
scp deploy/net-head/interfaces dietpi@10.0.0.10:/tmp/
ssh dietpi@10.0.0.10 'sudo cp /etc/network/interfaces /etc/network/interfaces.bak &&
                      sudo cp /tmp/interfaces /etc/network/interfaces'
```

Take the backup. Getting this file wrong strands a Pi that lives inside the
robot's head, and the recovery is a screwdriver.

## eth0 is static now (R7a, 2026-09-09)

`eth0` is `inet static` at `10.0.0.10/24` — the same address the NUC's DHCP
reserved (MAC `e4:5f:01:49:60:b9`), pinned so the Pi no longer waits on the
NUC's DHCP at boot. **No gateway** on the eth0 stanza on purpose: head's default
route is its WiFi (`wlan0`), and the `/24` link route reaches the NUC regardless
— adding a gateway here would install a second default route the machine doesn't
currently have. The address does not change, so nothing downstream noticed.

It was converted live behind a boot self-heal (a one-shot systemd unit that
restored DHCP and rebooted if `10.0.0.1` was unreachable), which passed and was
then removed. `/etc/network/interfaces.dhcp.bak` on the Pi is the pre-change
DHCP config — restore it and reboot to go back:

```
ssh dietpi@10.0.0.10 'sudo cp /etc/network/interfaces.dhcp.bak /etc/network/interfaces && sudo reboot'
```

## Why it is tracked at all

Until 2026-08-15 the only copy was on the machine, and the machine had a bug in
it. The wlan0 stanza was `inet dhcp` *and* carried a static `address
192.168.0.100` / `gateway 192.168.0.1` — a network that does not exist anywhere
near this robot. ifupdown applied that gateway whenever wlan0 came up,
replacing eth0's working default route with an unreachable one; when wlan0 then
dropped, which it does, the Pi was left with no default route at all. It could
reach the robot LAN and nothing else, while DNS kept working because the NUC is
its resolver — so it looked online and was not, and `apt` simply hung.

## Not tracked, deliberately

`/etc/wpa_supplicant/wpa_supplicant.conf` holds WiFi passwords in the clear and
has no business in a git repository.

## The other half

The fallback access point this Pi raises when no saved network is in range lives
in `deploy/hotspot/` and installs itself. `inmoov-autohotspot` runs
`ifdown`/`ifup` on wlan0, so a change to the wlan0 stanza here is a change to
how that behaves.
