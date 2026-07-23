# WiFi fallback hotspot

The head is used at venues that don't always have WiFi. So that you can always
reach the control panel from a phone or laptop, `wlan0` falls back to being its
**own access point** when no known network is in range.

- **Known network in range** → `wlan0` joins it normally (as before).
- **No known network** → `wlan0` becomes an AP; join it and open the panel.

Your Bluetooth PAN (`pan-server.service`, `pan0` at `10.0.0.1`) is a separate,
always-on backup — see [Bluetooth backup](#bluetooth-backup) below.

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

## Bluetooth backup

`pan-server.service` runs a Bluetooth Personal Area Network (NAP) at
`10.0.0.1/24`. A laptop or Android phone can Bluetooth-tether to it and reach the
panel at `http://10.0.0.1:8080` even with no WiFi at all. Note that **iPhones do
not support joining a Bluetooth PAN**, which is why the WiFi AP above is the
primary fallback.

### The head↔chest link

The chest display Pi is a permanent client of that PAN, so the head can reach it
at a **fixed** address no matter what the venue's DHCP does. This is the link
`config/settings.json` → `display.host` points at.

| | address | MAC | unit |
|---|---|---|---|
| **Head** | `10.0.0.1` (`pan0`) | `E4:5F:01:49:60:BB` | `pan-server.service` |
| **Chest** | `10.0.0.2` (`bnep0`) | `DC:A6:32:C8:F6:17` | `pan-client.service` |

Source of truth is `deploy/pan/` in this repo. Install/reinstall:

```bash
sudo deploy/pan/install.sh head      # on the head
sudo deploy/pan/install.sh chest     # on the chest (reboot after first install)
```

The installer prints the one-time pairing recipe, which is not automated.

**Either machine can boot first**, and either can reboot without a human:

- Head first → it registers NAP and waits; the chest connects when it comes up.
- Chest first → `pan-client` retries every ~10s (`Restart=always`, and
  `StartLimitIntervalSec=0` so hours of retrying can never exhaust a start
  limit) until the head answers.
- **Head reboots mid-session** → this is the one systemd cannot handle alone.
  `bt-network` does not notice the peer vanishing: it keeps running with a stale
  `bnep0` still holding `10.0.0.2`, so `Restart=always` never fires and the link
  is silently dead. `pan-check.timer` on the chest pings the head once a minute
  and rebuilds the link when it stops answering — recovery takes ~35s.

```bash
sudo /usr/local/sbin/inmoov-pan-check    # force a check now
sudo journalctl -t inmoov-pan            # what the watchdog has been doing
ping 10.0.0.2                            # from the head: is the link alive?
```

### Gotchas

- **A stock DietPi image cannot do Bluetooth PAN on a Pi 4 at all.** There is no
  `brcm/BCM4345C0.hcd`, so the adapter never gets its firmware patch, keeps the
  ROM default address `AA:AA:AA:AA:AA:AA`, and registers almost no profiles.
  `bt-network` then asserts (`SEGV`) instead of reporting an error. Fixed by the
  `bluez-firmware` + `pi-bluetooth` packages the installer pulls in; a **reboot**
  is required because the patch only loads during HCI device setup.
- **`bthelper` needs the extra udev rule** in `deploy/pan/`. Upstream's rule is
  gated on `/dev/serial1`, which DietPi never creates.
- **Re-pair after any adapter MAC change.** Changing the BD address gives BlueZ a
  fresh identity and silently invalidates both link keys.
- **Scan on the BR/EDR transport when pairing.** A default `bluetoothctl scan on`
  is LE-only and will not find the other Pi, even though `hcitool scan` does.
