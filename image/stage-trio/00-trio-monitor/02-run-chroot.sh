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
