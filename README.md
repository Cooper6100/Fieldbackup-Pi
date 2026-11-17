# FieldBackup Pi

Portable Raspberry Pi 5 backup station for SD cards, USB camera media and DJI storage, with SHA-256 verification, a dedicated NVMe backup SSD, optional OLED, home SMB synchronization and conditional shutdown.

> **Beta software.** Test with disposable media and verify copies independently before trusting it with irreplaceable footage.

## Features
- Automatically imports FAT/exFAT SD and USB media with a `DCIM` folder
- Copies to `/mnt/media/SD-Backups` on a **separate NVMe backup filesystem**
- SHA-256 verifies new files, skips identical copies and never deletes source media
- Optional SSD1306 OLED and status LEDs
- Optional home SMB synchronization (five-minute timer)
- Optional automatic shutdown after a verified backup and 180 seconds idle on an approved away Wi-Fi profile
- Shutdown watchdog **disabled by default**; its default setting is **dry run**

## Requirements
Raspberry Pi 5 with 64-bit Raspberry Pi OS booting from NVMe, a separate mounted backup NVMe at `/mnt/media`, stable power, and a USB SD-card reader. The installer is **for a fresh Pi only** and refuses to overwrite an existing FieldBackup installation.

## Installation
```bash
git clone https://github.com/Cooper6100/Fieldbackup-Pi.git
cd Fieldbackup-Pi
sudo ./install.sh
sudo ./configure-smb.sh  # optional
```

**Before installing:** mount your dedicated backup NVMe at `/mnt/media` using its own UUID in `/etc/fstab`. Never format a disk without checking its contents and identity.

For SMB and shutdown, copy `payload/fieldbackup-network.example.json` to `/etc/fieldbackup-network.json` and configure your own home network and approved hotspot profiles. **Never commit your local configuration or credentials.**

See the [full installation and operating manual](docs/MANUAL.md) for mount setup, network configuration, logs, safe shutdown activation, and troubleshooting.

## Safety
- The optional watchdog is not automatically enabled and starts in dry-run mode.
- A verified backup is not a substitute for keeping an independent copy.
- Source media are never intentionally deleted.
- Cards mounted read-only by another service are not automatically unmounted by the importer.
- SMB synchronization requires a share supporting the file operations used by the sync script.
- UPS telemetry and low-battery shutdown are not implemented.

## License
MIT — see [LICENSE](LICENSE).
