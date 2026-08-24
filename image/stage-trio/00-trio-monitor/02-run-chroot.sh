#!/bin/bash -e
# The first user ("trio") is created by pi-gen (username input in the
# workflow); config and database live in its home so /opt stays pristine.
chown -R root:root /opt/trio-monitor

systemctl enable trio-monitor.service
systemctl set-default multi-user.target

# Wall display: never blank the console.
if ! grep -q consoleblank /boot/firmware/cmdline.txt; then
	sed -i '1 s/$/ consoleblank=0/' /boot/firmware/cmdline.txt
fi

# ---- slim the image ----
# No Bluetooth on a wall display; onboard Wi-Fi is Broadcom, so firmware
# for USB dongle chipsets (Atheros/Realtek/Libertas) can go too.
for pkg in bluez firmware-atheros firmware-realtek firmware-libertas \
	modemmanager triggerhappy; do
	apt-get -y purge "$pkg" 2>/dev/null || true
done
apt-get -y autoremove --purge
apt-get clean

# Docs, man pages, and non-English locales are dead weight on an appliance.
rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/info/*
find /usr/share/locale -mindepth 1 -maxdepth 1 ! -name 'en*' \
	-exec rm -rf {} + 2>/dev/null || true
rm -rf /var/lib/apt/lists/*
