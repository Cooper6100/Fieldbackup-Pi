# FieldBackup Pi — User and Technical Manual

> **Beta release (November 2026).** This repository contains the updated two-NVMe design, with optional SMB synchronization and conditional shutdown. Test with nonessential media before depending on it. The public watchdog defaults to dry-run and is not enabled by the installer.

## 1. What it does
FieldBackup Pi is a Raspberry Pi 5 camera-media ingest station. It detects FAT/exFAT SD cards and USB mass-storage volumes with a `DCIM` folder, copies to NVMe, verifies new files using SHA-256, and skips files already present with identical content. It never deletes source files. Optional OLED and GPIO LEDs display progress. Optional home SMB synchronization and conditional shutdown are separate services.

## 2. Hardware
- Raspberry Pi 5 with 64-bit Raspberry Pi OS booting from NVMe
- **Separate NVMe** backup filesystem mounted at `/mnt/media`, with backups in `/mnt/media/SD-Backups`
- Reliable USB-C power, USB SD reader and optional SSD1306 128×64 I²C OLED (address 0x3c)
- Optional LEDs with suitable resistors: blue BCM17, green BCM27, red BCM22
- Check enclosure/HAT, PCIe and power compatibility for two NVMe drives

## 3. Fresh installation
1. Boot the Pi from its OS NVMe. Independently prepare a backup NVMe, mount it at `/mnt/media` and configure its filesystem UUID in `/etc/fstab`. **Do not format a drive containing valuable data.**
2. Clone the repository and run `sudo ./install.sh`. The installer is **fresh-install only** and refuses to overwrite an existing deployment.
3. The installer checks both NVMe mounts, creates `/mnt/media/SD-Backups`, records the backup filesystem UUID in `/etc/sd-field-backup.json`, and installs the importer, OLED, SMB timer and shutdown watchdog service files.
4. Reboot if I²C was newly enabled. Confirm the importer with `systemctl status sd-field-backup.service`.

The optional shutdown watchdog is installed **disabled**, with `DRY_RUN = True`.

## 4. Backup behavior
Insert a FAT/exFAT card or USB drive with `DCIM`. The importer reads the source, stores files under `/mnt/media/SD-Backups/YYYY-MM-DD/<volume-id>/` (some DJI devices use a stable device folder), verifies SHA-256, and refuses conflicting destination files. A minimum free-space reserve is enforced. The shared transfer lock prevents FieldBackup's importer and SMB sync from overlapping.

If the importer mounted a device itself, it unmounts it after verification. If another service already mounted it read-only, it leaves that mount alone. Therefore **“Backup completed safely” does not necessarily mean a pre-mounted card was unmounted**.

View logs: `sudo journalctl -u sd-field-backup.service -f`.

## 5. Optional SMB home synchronization
Run `sudo ./configure-smb.sh` to provide the share, username and password. Credentials are saved in `/etc/fieldbackup-smb.credentials`, not in the repository. The timer runs periodically, checks the backup SSD UUID and configured home network, and copies/validates files on the SMB share. It never deletes source files.

Create `/etc/fieldbackup-network.json` from `payload/fieldbackup-network.example.json` and fill in **your own** network profile names, router IP and MAC address. The network fields are not credentials but are private deployment details.

The sync script uses temporary files and hard links: some SMB servers do not support this; verify your target. Check `sudo journalctl -u fieldbackup-sync.service -n 50 --no-pager`.

## 6. Optional automatic shutdown
The watchdog observes **new** importer journal events and waits for backup start, verification, completion, a free transfer lock and 180 seconds idle. It only permits shutdown on an explicitly approved away Wi-Fi **NetworkManager connection profile**; home and unknown/offline network states inhibit shutdown. It also checks importer availability, backup SSD UUID and that the SMB sync service is inactive.

Example `/etc/fieldbackup-network.json`:
```json
{
  "home_ssid": "YOUR_HOME_WIFI_PROFILE",
  "home_router_ip": "192.168.1.1",
  "home_router_mac": "00:00:00:00:00:00",
  "away_ssids": ["YOUR_PHONE_HOTSPOT_PROFILE"]
}
```

**Note:** Connection profile names may differ from the actual broadcast SSIDs. Profile/SSID matching and router MAC checks are not cryptographic authentication. A poweroff can interrupt unrelated applications outside FieldBackup's transfer lock.

The installer leaves the watchdog disabled and its script in `DRY_RUN = True` mode. To test deliberately, run `sudo systemctl enable --now fieldbackup-shutdown.service`, perform a new backup on an approved away network, and inspect `sudo journalctl -u fieldbackup-shutdown.service -f`. A successful dry run logs `DRY RUN: Would shut down now`. Only after independently validating the deployment should an operator change `DRY_RUN = False` and restart the service.

## 7. Existing deployments
**Do not rerun the fresh installer on an operating Pi.** Back up your local scripts, service units and `/etc` configuration and migrate changes individually. In particular, never replace your own backup filesystem UUID, home network configuration or SMB credentials with example values.

## 8. Troubleshooting
- Importer: `journalctl -u sd-field-backup.service -n 100`; inspect `lsblk -f` and the source `DCIM` folder.
- Backup SSD: `findmnt -T /mnt/media/SD-Backups -o SOURCE,UUID,TARGET`.
- OLED: `sudo i2cdetect -y 1` and `journalctl -u fieldbackup-oled.service`.
- SMB: `systemctl status fieldbackup-sync.timer` and `journalctl -u fieldbackup-sync.service`.
- Shutdown: `journalctl -u fieldbackup-shutdown.service`; check the active NetworkManager connection profile, successful *new* backup, and idle period.

## 9. Safety and privacy
Keep original camera media until an independent copy has been checked. Do not publish `/etc/fieldbackup-smb.credentials`, `/etc/fieldbackup-smb.conf`, `/etc/fieldbackup-network.json`, `/etc/sd-field-backup.json`, backup media, or private network identifiers. This release does not include UPS telemetry or automatic low-battery shutdown.
