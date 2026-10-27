#!/usr/bin/env bash
set -Eeuo pipefail

packages=(python3 python3-serial usbutils)
modules_package="linux-modules-extra-$(uname -r)"

sudo apt-get update
if apt-cache show "$modules_package" >/dev/null 2>&1; then
    packages+=("$modules_package")
fi
sudo apt-get install -y "${packages[@]}"

sudo modprobe usbserial
sudo modprobe ch341
sudo modprobe ftdi_sio

python3 -c 'import serial; print("pyserial", serial.VERSION)'
lsusb

