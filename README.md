# Trio Monitor

A wall-mounted glucose dashboard for two (or more) people, built for a
Raspberry Pi with the official 7" touchscreen. It boots straight into the
display — no desktop environment — and shows each person's current glucose,
trend, a 3-hour history chart, a 2-hour forecast, insulin on board, carbs on
board, and recent treatments.

![Screenshot](docs/screenshot.png)

## Features

- **Per-person data sources** — each person's data can arrive by:
  - **Push**: point a [Trio](https://github.com/nightscout/Trio) (or any
    Nightscout uploader) at the Pi — it speaks a minimal Nightscout v1 API,
    one port per person
  - **Pull from Tidepool** — for the twiist AID system, which uploads to
    Tidepool automatically; readings, boluses, carbs, IOB/COB, and pump
    settings all come across
  - **Pull from a Nightscout site** — for people with an existing cloud
    Nightscout; API secrets and access tokens both work (auto-detected)
- **Glucose forecasts** (30m / 1h / 1.5h / 2h): uses the AID system's own
  prediction curve when available (Trio's `predBGs`, Loop's `predicted`),
  otherwise runs an oref0-style model (exponential insulin activity,
  deviation-based carb absorption) with therapy settings pulled from the
  person's Nightscout profile or Tidepool pump settings. Estimates are
  marked with `~`.
- **Web app** — the same dashboard in any browser, auto-refreshing,
  responsive, with light/dark themes. Settings (people, sources, thresholds)
  and a sync log are managed from the browser too; no SSH needed after
  install. Works great through a Cloudflare tunnel.
- **Touchscreen light/dark mode** — tap the sun/moon on the display.
- **Per-person thresholds** — low/high/urgent ranges per person, with
  global defaults.
- **QR-code onboarding** — a fresh device shows a QR code that takes a phone
  to the setup page; with no network at all it opens its own setup hotspot
  first so you can connect the Pi to Wi-Fi from your phone.

## Install

### Option A: flash the ready-made image

Grab `trio-monitor-<version>.img.xz` from the
[releases page](https://github.com/hwhitfield2/Trio-Monitor/releases),
flash it with Raspberry Pi Imager (or `dd`), boot the Pi, and follow the
QR codes on screen. That's the whole install.

(Images are built by the `Build SD card image` GitHub Actions workflow —
push a `v*` tag or run it manually.)

### Option B: install on an existing Raspberry Pi OS

Use Raspberry Pi OS **Lite** (no desktop needed):

```bash
curl -sSL https://raw.githubusercontent.com/hwhitfield2/Trio-Monitor/main/install.sh | bash
```

The installer handles everything: dependencies, config with random secrets,
console screen-blanking, and a systemd service that starts on boot. It
finishes by printing the URL and API secret for each person's uploader.

## Connecting the data

- **Trio (push)**: in Trio, Settings → Services → Nightscout, set URL
  `http://<pi-ip>:<port>` and the person's API secret (both shown by the
  installer and on the web settings page).
- **twiist**: the wearer links their My twiist Portal account to a free
  [Tidepool](https://www.tidepool.org) account (one-time), then enter the
  Tidepool login in the web settings under their data source.
- **Nightscout**: enter the site URL and its API secret or access token.

## The web app

Everything is served from the admin port (default 8080, HTTP Basic auth):

| Path | What |
|---|---|
| `/` | Live dashboard (auto-refreshes every 30s) |
| `/settings` | People, data sources, thresholds, Wi-Fi |
| `/log` | Sync activity from every data source |
| `/screen.png` | What the physical screen shows right now |
| `/api/dashboard.json` | The dashboard's data, as JSON |

## Development

Runs on a Mac/PC in a window with fake data:

```bash
pip install pygame qrcode
python -m trio_monitor --demo --windowed
```

`--no-display` runs the servers headless; `--screenshot out.png` renders one
frame and exits. The only runtime dependencies are `pygame` and `qrcode`
(both from apt on the Pi); everything else is the Python standard library.

## Safety note

This is a convenience display, not a medical device. Forecasts are estimates
— even the pump-provided ones. Don't rely on it for alarms or treatment
decisions; use the CGM app's own alerts for that.
