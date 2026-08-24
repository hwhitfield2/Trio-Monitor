"""Wi-Fi provisioning via NetworkManager (nmcli).

When the Pi has no network at all, we bring up a setup hotspot
("TrioMonitor-Setup"). The display shows a WIFI: QR code that joins a
phone to that hotspot, where the settings page offers a list of nearby
networks so the user can hand the Pi their home Wi-Fi credentials.

All calls go through nmcli, which is present on Raspberry Pi OS
(Bookworm and later). On systems without it — e.g. a dev Mac — every
probe degrades to "unknown"/no-op so the rest of the app is unaffected.
"""

import logging
import shutil
import subprocess
import threading

from . import synclog

log = logging.getLogger("trio_monitor.network")

HOTSPOT_SSID = "TrioMonitor-Setup"
HOTSPOT_CONN = "trio-monitor-hotspot"
HOTSPOT_ADDR = "10.42.0.1"


def _nmcli(*args, timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["nmcli", *args], capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


def available() -> bool:
    return shutil.which("nmcli") is not None


def connectivity() -> str:
    """'full' | 'limited' | 'portal' | 'none' | 'unknown'.

    Raspberry Pi OS ships with NetworkManager's connectivity checking
    disabled, which reports 'unknown' — fall back to checking for a
    default route so 'none' (and with it the setup hotspot) still works.
    """
    if not available():
        return "unknown"
    code, out = _nmcli("networking", "connectivity", "check")
    state = out.splitlines()[-1].strip() if code == 0 and out else "unknown"
    if state != "unknown":
        return state
    try:
        proc = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=10,
        )
        return "limited" if proc.stdout.strip() else "none"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def hotspot_active() -> bool:
    code, out = _nmcli("-t", "-f", "NAME", "connection", "show", "--active")
    return code == 0 and HOTSPOT_CONN in out.splitlines()


def start_hotspot(password: str) -> bool:
    code, out = _nmcli(
        "device", "wifi", "hotspot",
        "con-name", HOTSPOT_CONN, "ssid", HOTSPOT_SSID, "password", password,
    )
    if code == 0:
        log.info("Setup hotspot '%s' started", HOTSPOT_SSID)
        synclog.add("network", "system", f"setup hotspot '{HOTSPOT_SSID}' started")
    else:
        log.warning("Could not start hotspot: %s", out)
        synclog.add("network", "system", f"hotspot failed: {out}", ok=False)
    return code == 0


def stop_hotspot() -> None:
    _nmcli("connection", "down", HOTSPOT_CONN)
    _nmcli("connection", "delete", HOTSPOT_CONN)


def wifi_scan() -> list[dict]:
    """Nearby networks, strongest first, deduplicated by SSID."""
    code, out = _nmcli(
        "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
        "--rescan", "auto", timeout=30,
    )
    if code != 0:
        return []
    seen, networks = set(), []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3 or not parts[0] or parts[0] == HOTSPOT_SSID:
            continue
        ssid = parts[0]
        if ssid in seen:
            continue
        seen.add(ssid)
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        networks.append({
            "ssid": ssid,
            "signal": signal,
            "secured": bool(parts[2] and parts[2] != "--"),
        })
    return sorted(networks, key=lambda n: -n["signal"])


def connect_wifi(ssid: str, password: str) -> tuple[bool, str]:
    """Leave the hotspot (if up) and join the given network."""
    if hotspot_active():
        stop_hotspot()
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    code, out = _nmcli(*args, timeout=60)
    if code == 0:
        log.info("Joined Wi-Fi network '%s'", ssid)
        synclog.add("network", "system", f"joined Wi-Fi '{ssid}'")
        return True, out
    log.warning("Failed to join '%s': %s", ssid, out)
    synclog.add("network", "system", f"failed to join '{ssid}': {out}", ok=False)
    return False, out


class NetworkWatcher(threading.Thread):
    """Brings the setup hotspot up when the device has no network at all.

    Requires three consecutive failed checks (~90s) so brief outages and
    router reboots don't tear down normal networking. 'none' means no
    connection whatsoever — LAN-only setups report 'limited' and are
    left alone.
    """

    CHECK_SECONDS = 30
    FAILS_NEEDED = 3

    def __init__(self, hotspot_password: str):
        super().__init__(name="network-watcher", daemon=True)
        self.hotspot_password = hotspot_password
        self._fails = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not available():
            log.info("nmcli not found; Wi-Fi provisioning disabled")
            return
        while not self._stop.is_set():
            state = connectivity()
            if hotspot_active():
                self._fails = 0  # we're in setup mode; stay until joined
            elif state == "none":
                self._fails += 1
                if self._fails >= self.FAILS_NEEDED:
                    start_hotspot(self.hotspot_password)
            else:
                self._fails = 0
            self._stop.wait(self.CHECK_SECONDS)
