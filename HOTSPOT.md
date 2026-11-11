# WiFi fallback hotspot

> **Read first (2026-09-09):** the brain is the **NUC** now, not the head. The
> control panel lives at `10.0.0.1:8080` on the NUC, which serves the wired robot
> LAN and its own `fred` access point; the head and chest are static clients
> (`10.0.0.10` / `10.0.0.11`). The **Bluetooth PAN described below has been
> retired** — it put `10.0.0.1` on the head and would now collide with the NUC.
> For the current network map, see the Networking section of `README.md`
> (source of truth) and `deploy/net-head/`, `deploy/net-chest/`. What remains
> accurate here is the head's own `wlan0` AP-fallback mechanics.


The head is used at venues that don't always have WiFi. So that you can always
reach the control panel from a phone or laptop, `wlan0` falls back to being its
**own access point** when no known network is in range.

- **Known network in range** → `wlan0` joins it normally (as before).
- **No known network** → `wlan0` becomes an AP; join it and open the panel.

## Joining the fallback AP

| | |
|---|---|
| **Network (SSID)** | `InMoov-FRED` |
| **Password** | `inmoov-robot` |
| **Panel URL** | `http://192.168.50.1:8080` (or `http://inmoov.local:8080`) |

Works with any phone or laptop, including iPhones. There is **no internet** while
on the AP — it only serves the LAN so you can drive the robot.

## How it works

- `inmoov-hotspot.service` runs once at boot: it waits ~30s for the normal join,
  and if that fails brings up the AP (`hostapd` + `dnsmasq`).
- `inmoov-hotspot.timer` re-checks every ~2.5 min so it recovers if the network
  drops (or comes back) mid-session. It will **not** drop the AP while a device
  is connected to it — only when the hotspot is idle and a known network returns.
- Everything is driven by `/usr/local/sbin/inmoov-autohotspot`. The packaged
  `hostapd`/`dnsmasq` system services are disabled on purpose; this script is the
  single owner of `wlan0`'s role.

Known networks are read straight from `/etc/wpa_supplicant/wpa_supplicant.conf`,
so adding a venue's WiFi there (via `dietpi-config` or by editing the file) is all
it takes for the head to prefer it over the AP.

## Manual control

```bash
sudo inmoov-autohotspot status     # CLIENT / ACCESS POINT / DISCONNECTED
sudo inmoov-autohotspot ap         # force the hotspot on now
sudo inmoov-autohotspot client     # force rejoining known networks
sudo journalctl -t inmoov-hotspot  # what the failover has been doing
```

## Changing the SSID / password

Edit the values in `deploy/hotspot/hostapd.conf`, then re-run the installer:

```bash
sudo deploy/hotspot/install.sh
```

(or edit `/etc/hostapd/inmoov-hostapd.conf` directly and reboot). The password
must be 8–63 characters.

## Testing it

The AP can't be exercised without taking `wlan0` off the network it's on, so the
real test is a **reboot where none of your saved networks exist** (e.g. power it
up away from home/office, or temporarily rename your saved SSIDs):

1. Boot the head with no known WiFi around.
2. On a phone, join **`InMoov-FRED`** (password `inmoov-robot`).
3. Open **`http://192.168.50.1:8080`** — the panel should load.

Then bring a known network back and reboot; it should rejoin as a client.

## Install / uninstall

- **Install:** `sudo deploy/hotspot/install.sh` (idempotent).
- **Uninstall:**
  ```bash
  sudo systemctl disable --now inmoov-hotspot.service inmoov-hotspot.timer
  sudo rm /etc/systemd/system/inmoov-hotspot*.service /etc/systemd/system/inmoov-hotspot.timer
  sudo rm /usr/local/sbin/inmoov-autohotspot
  sudo systemctl daemon-reload
  ```

## Bluetooth backup — RETIRED

The head↔chest Bluetooth PAN (`pan-server`/`pan-client`, head `10.0.0.1`, chest
`10.0.0.2`) belonged to the era when the head was the brain. It is gone: the NUC
serves the robot LAN over wired ethernet and both Pis are static on it. Do not
enable `pan-server.service` — `10.0.0.1` is the NUC's now. The old units and
their long-fought pairing notes are preserved for the record under
`deploy/retired/pan/`.
