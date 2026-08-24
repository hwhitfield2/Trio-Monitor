#!/bin/bash -e
# Copy the application into the image. pi-gen only mounts the stage
# directory into its build container, so the workflow copies the
# trio_monitor package into files/ before the build starts.
FILES="${STAGE_DIR}/00-trio-monitor/files"

install -d "${ROOTFS_DIR}/opt/trio-monitor"
cp -r "${FILES}/trio_monitor" "${ROOTFS_DIR}/opt/trio-monitor/"
find "${ROOTFS_DIR}/opt/trio-monitor" -name __pycache__ -type d -exec rm -rf {} + || true

install -m 644 "${FILES}/trio-monitor.service" \
	"${ROOTFS_DIR}/etc/systemd/system/trio-monitor.service"

install -D -m 644 "${FILES}/50-trio-monitor.rules" \
	"${ROOTFS_DIR}/etc/polkit-1/rules.d/50-trio-monitor.rules"
