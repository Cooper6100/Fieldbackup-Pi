# FieldBackup Pi

Portable, checksum-verified SD-card, USB-media and DJI camera backups for a Raspberry Pi 5 booting from NVMe.

> **v0.1.0 beta — untested on a clean Pi.** The current hardware configuration uses an SSD1306 128×64 I²C OLED (0x3c) and three LEDs on GPIO BCM 17/27/22. Argon UPS telemetry and low-battery shutdown are **not implemented**.

## Features
- Automatically detects USB FAT/exFAT partitions and whole-disk volumes, including DJI Neo 2 mass storage.
- Copies `DCIM` content from sources mounted read-only to NVMe, SHA-256-verifying every new copy; skips identical destination files.
- DJI Neo 2 uses a fixed destination for incremental backup. Other media use date/volume folders (incremental within each day only).
- Safe unmount, status LEDs, OLED status and optional five-minute SMB synchronization.
- No automatic deletion of source media.

## Requirements
Raspberry Pi 5, 64-bit Raspberry Pi OS with root filesystem on NVMe, adequate power, and internet access for apt. Optional SSD1306 OLED and LEDs need appropriate wiring and current-limiting resistors. Check GPIO conflicts with any HAT or case before connecting.

## Installation
```bash
sudo ./install.sh
sudo ./configure-smb.sh  # optional; asks for SMB share and credentials
```
Reboot if the installer enables I²C. Check `systemctl status sd-field-backup.service fieldbackup-oled.service fieldbackup-sync.timer` and `journalctl -u sd-field-backup.service -f`.

## Backup destination
`/srv/sd-backups` on the same NVMe root filesystem. The installer sets the root filesystem UUID automatically and refuses installation over an existing FieldBackup deployment.

## Important safety notes
- **Beta software**: test with expendable media and verify files independently before relying on it.
- Sync is optional. Its destination must support the temporary-file and hard-link operations used by the current sync script; some SMB servers may not. The sync feature has not been validated across SMB implementations.
- Device matching is broad: any USB FAT/exFAT volume can be examined, but only media with a `DCIM` directory are copied. Devices lacking `DCIM` report an error.
- Do not disconnect power or media during a transfer. The UPS is power-only until separately integrated and tested.
- Configuration and credentials live in `/etc`, not in this repository. Never commit private files, tokens or media.

## Contributing
Issues and pull requests are welcome. Please remove personal data from logs and screenshots before posting.

## License
MIT. See [LICENSE](LICENSE).
