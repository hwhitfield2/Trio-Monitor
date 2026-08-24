#!/usr/bin/env bash
# One-shot installer for Trio Monitor on a Raspberry Pi.
#
# Run from a checkout:   ./install.sh
# Or with nothing yet:   curl -sSL https://raw.githubusercontent.com/hwhitfield2/Trio-Monitor/main/install.sh | bash
#
# Installs all dependencies, generates config.json with random API secrets,
# disables console screen blanking, and enables + starts the boot service.
set -euo pipefail

REPO_URL="${TRIO_MONITOR_REPO:-https://github.com/hwhitfield2/Trio-Monitor.git}"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

log() { echo "==> $*"; }

# --- Locate (or fetch) the repo -------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd)"
if [ ! -f "$SCRIPT_DIR/trio_monitor/__main__.py" ]; then
    # Piped from curl or run outside a checkout: clone and continue from there.
    REPO_DIR="$HOME/Trio-Monitor"
    if [ ! -d "$REPO_DIR/.git" ]; then
        log "Cloning $REPO_URL to $REPO_DIR"
        command -v git >/dev/null || { $SUDO apt-get update; $SUDO apt-get install -y git; }
        git clone "$REPO_URL" "$REPO_DIR"
    fi
else
    REPO_DIR="$SCRIPT_DIR"
fi
cd "$REPO_DIR"
RUN_USER="${SUDO_USER:-$(whoami)}"

# --- Dependencies ----------------------------------------------------------

log "Installing dependencies (python3, pygame, qrcode)"
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-pygame python3-qrcode

if ! python3 -c "import pygame" 2>/dev/null; then
    log "apt pygame unavailable; falling back to pip"
    $SUDO apt-get install -y python3-pip
    python3 -m pip install --user --break-system-packages pygame \
        || python3 -m pip install --user pygame
fi

# --- Config with auto-generated secrets ------------------------------------

if [ ! -f "$REPO_DIR/config.json" ]; then
    log "Creating config.json with random API secrets"
    python3 - "$REPO_DIR" <<'PYEOF'
import json, secrets, sys
from pathlib import Path

repo = Path(sys.argv[1])
config = json.loads((repo / "config.example.json").read_text())
for user in config["users"]:
    user["api_secret"] = secrets.token_hex(12)
(repo / "config.json").write_text(json.dumps(config, indent=2) + "\n")
PYEOF
else
    log "config.json already exists; keeping it"
fi

# --- Keep the screen awake (wall display) ----------------------------------

for CMDLINE in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [ -f "$CMDLINE" ]; then
        if ! grep -q "consoleblank=0" "$CMDLINE"; then
            log "Disabling console screen blanking in $CMDLINE"
            $SUDO sed -i '1 s/$/ consoleblank=0/' "$CMDLINE"
        fi
        break
    fi
done

# --- systemd service -------------------------------------------------------

log "Installing systemd service (user: $RUN_USER, path: $REPO_DIR)"
sed -e "s|^User=.*|User=$RUN_USER|" \
    -e "s|^Group=.*|Group=$RUN_USER|" \
    -e "s|/home/pi/Trio-Monitor|$REPO_DIR|g" \
    "$REPO_DIR/systemd/trio-monitor.service" \
    | $SUDO tee /etc/systemd/system/trio-monitor.service > /dev/null

$SUDO systemctl daemon-reload
$SUDO systemctl enable trio-monitor.service

if [ -d /dev/dri ]; then
    log "Starting trio-monitor"
    $SUDO systemctl restart trio-monitor.service || true
else
    log "No display hardware detected; service will start on next boot"
fi

# --- Summary ---------------------------------------------------------------

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================================"
echo " Trio Monitor installed."
echo
echo " Enter these in each Trio under Settings -> Services -> Nightscout:"
python3 - "$REPO_DIR" "${IP:-<pi-ip>}" <<'PYEOF'
import json, sys
from pathlib import Path

config = json.loads((Path(sys.argv[1]) / "config.json").read_text())
ip = sys.argv[2]
for user in config["users"]:
    print(f"   {user['name']}:")
    print(f"     URL:        http://{ip}:{user['port']}")
    print(f"     API secret: {user['api_secret']}")
PYEOF
echo
echo " Edit the names in $REPO_DIR/config.json, then:"
echo "   sudo systemctl restart trio-monitor"
echo
echo " Logs: journalctl -u trio-monitor -f"
echo " If screen blanking was just disabled, reboot once for it to apply."
echo "============================================================"
