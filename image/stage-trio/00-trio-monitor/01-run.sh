#!/bin/bash -e
# Copy the application into the image. STAGE_DIR is image/stage-trio inside
# the repo checkout, so the repo root is two levels up.
REPO_DIR="$(cd "${STAGE_DIR}/../.." && pwd)"

install -d "${ROOTFS_DIR}/opt/trio-monitor"
cp -r "${REPO_DIR}/trio_monitor" "${ROOTFS_DIR}/opt/trio-monitor/"
find "${ROOTFS_DIR}/opt/trio-monitor" -name __pycache__ -type d -exec rm -rf {} + || true

install -m 644 "${STAGE_DIR}/00-trio-monitor/files/trio-monitor.service" \
	"${ROOTFS_DIR}/etc/systemd/system/trio-monitor.service"
