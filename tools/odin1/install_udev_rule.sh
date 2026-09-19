#!/usr/bin/env bash
set -euo pipefail

RULE_FILE=/etc/udev/rules.d/99-odin-usb.rules
RULE='SUBSYSTEM=="usb", ATTR{idVendor}=="2207", ATTR{idProduct}=="0019", MODE="0666", GROUP="plugdev"'

printf '%s\n' "${RULE}" | sudo tee "${RULE_FILE}" >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=2207 --attr-match=idProduct=0019
echo "Installed ${RULE_FILE}. Power-cycle or reconnect Odin1 if its permissions did not change."
