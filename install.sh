#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ $EUID -eq 0 ]] || { echo 'Run with sudo: sudo ./install.sh'; exit 1; }
[[ -d payload ]] || { echo 'Missing payload directory'; exit 1; }
[[ $(uname -m) == aarch64 ]] || { echo 'Requires 64-bit Raspberry Pi OS'; exit 1; }
ROOT_SOURCE=$(findmnt -n -o SOURCE -T /)
ROOT_DEVICE=$(readlink -f "$ROOT_SOURCE")
[[ $ROOT_DEVICE == /dev/nvme* ]] || { echo "STOP: / is not on NVMe ($ROOT_SOURCE)"; exit 1; }
ROOT_UUID=$(findmnt -n -o UUID -T /)
[[ -n $ROOT_UUID ]] || { echo 'Cannot determine root UUID'; exit 1; }
[[ -d /etc/systemd/system ]] || exit 1
if systemctl is-active --quiet sd-field-backup.service; then
  echo 'STOP: backup service is running; this installer is intended for a fresh Pi.'
  exit 1
fi
if [[ -e /opt/sd-field-backup/backup.py || -e /opt/fieldbackup-oled/display.py ]]; then
  echo 'STOP: existing FieldBackup detected; refusing to overwrite it.'
  exit 1
fi
printf 'Target root: %s (UUID %s)\n' "$ROOT_DEVICE" "$ROOT_UUID"
read -r -p 'Install FieldBackup on this NVMe Pi? [y/N] ' ANSWER
[[ $ANSWER == [yY] ]] || exit 0
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-gpiozero python3-lgpio python3-pil python3-luma.oled python3-smbus i2c-tools cifs-utils rsync exfatprogs util-linux samba
# Ensure I2C enabled, preserve other boot settings.
BOOTCFG=/boot/firmware/config.txt
if [[ -f $BOOTCFG ]] && ! grep -Eq '^dtparam=i2c_arm=on' "$BOOTCFG"; then
  printf '\n# FieldBackup OLED\ndtparam=i2c_arm=on\n' >> "$BOOTCFG"
  echo 'I2C enabled in config.txt; reboot required for OLED.'
fi
install -d -m 755 /opt/sd-field-backup /opt/fieldbackup-oled /opt/fieldbackup-sync /srv/sd-backups /mnt/home-smb /var/lib/fieldbackup-sync
install -m 755 payload/sd-field-backup/backup.py /opt/sd-field-backup/backup.py
install -m 755 payload/fieldbackup-oled/display.py /opt/fieldbackup-oled/display.py
install -m 755 payload/fieldbackup-sync/sync.sh /opt/fieldbackup-sync/sync.sh
install -m 644 payload/sd-field-backup.json /etc/sd-field-backup.json
python3 - "$ROOT_UUID" <<'PYCONFIG'
import json,sys
p='/etc/sd-field-backup.json'
d=json.load(open(p)); d['root_uuid']=sys.argv[1]
with open(p,'w') as f:json.dump(d,f,indent=2);f.write('\n')
PYCONFIG
for unit in sd-field-backup.service fieldbackup-oled.service fieldbackup-sync.service fieldbackup-sync.timer; do
  install -m 644 "payload/$unit" "/etc/systemd/system/$unit"
done
# Avoid enabling sync without credentials. Prompt user separately.
if [[ ! -f /etc/fieldbackup-smb.credentials ]]; then
  echo 'SMB credentials missing. Configure later with sudo ./configure-smb.sh'
fi
python3 -m py_compile /opt/sd-field-backup/backup.py /opt/fieldbackup-oled/display.py
bash -n /opt/fieldbackup-sync/sync.sh
systemctl daemon-reload
systemctl enable sd-field-backup.service fieldbackup-oled.service
systemctl start sd-field-backup.service
if [[ -f /etc/fieldbackup-smb.credentials && -f /etc/fieldbackup-smb.conf ]]; then
  chmod 600 /etc/fieldbackup-smb.credentials
  systemctl start fieldbackup-sync.timer
else
  echo 'SMB sync disabled until configure-smb.sh is run.'
  systemctl disable fieldbackup-sync.timer
fi
if python3 -c 'from luma.oled.device import ssd1306; from luma.core.interface.serial import i2c' >/dev/null 2>&1; then
  systemctl start fieldbackup-oled.service || true
else
  echo 'WARNING: OLED Python library not importable; check OS packages.'
fi
# Samba LAN share is opt-in to avoid changing any existing configuration.
echo
printf 'Installed FieldBackup. Root UUID: %s\n' "$ROOT_UUID"
echo 'Configure SMB credentials: sudo ./configure-smb.sh'
echo 'Check: systemctl status sd-field-backup.service fieldbackup-oled.service'
echo 'Check OLED I2C: sudo i2cdetect -y 1 (should show 3c)'
echo 'Reboot if I2C was newly enabled.'
