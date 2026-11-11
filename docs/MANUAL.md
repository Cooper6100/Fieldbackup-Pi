# FieldBackup Pi — Full User and Technical Manual

> **Status (November 2026): beta.** This document distinguishes the code currently published in this repository from enhancements tested on the original author's Pi. **Do not assume that cloning this repository installs the author's latest two-NVMe configuration or shutdown watchdog.** Test with disposable media first.

## 1. What is FieldBackup Pi?

FieldBackup Pi is a small, unattended media-ingest station based on a Raspberry Pi 5. Insert a camera SD/microSD card or connect a USB mass-storage device containing a `DCIM` folder. The importer copies the media to NVMe storage, checks each copied file with SHA-256, skips files already present with matching content, and reports progress on an optional OLED.

It is aimed at drone pilots, photographers and videographers who want a backup without a laptop. It **does not** erase or format source cards, and a successful backup is not a substitute for keeping another independent copy.

## 2. Hardware

The author's field setup:

| Part | Purpose |
| --- | --- |
| Raspberry Pi 5, 16 GB | Host computer |
| Argon NEO 5 M.2 enclosure | Compact case / NVMe arrangement; check exact hardware expansion compatibility |
| 2 TB NVMe | Boot / OS |
| Separate 1 TB NVMe | Backup destination (author's *current* Pi) |
| USB SD/microSD reader | Camera-card ingest |
| SSD1306 128×64 I²C OLED, address 0x3c | Local status |
| Suitable USB-C supply / power bank | Portable power |

**Important:** The public installer currently supports **a single NVMe root-filesystem destination**, `/srv/sd-backups`. The author's newer deployment instead stores backups on a second, independently mounted NVMe. Do not copy a mount path or UUID from someone else's installation. Verify that your enclosure, M.2 adapter/HAT, PCIe connections, power supply and cooling support your chosen two-drive setup.

The OLED is optional; GPIO LEDs (BCM 17/27/22 in the published configuration) are optional and require correct wiring, resistors and conflict checks. The author's current build relies on the OLED, not LEDs. No integrated UPS/battery telemetry is implemented.

## 3. Published software vs author's newer deployment

| Feature | Current public repository | Author's current Pi |
| --- | --- | --- |
| Raspberry Pi 5, 64-bit OS | Yes | Yes |
| USB FAT/exFAT ingest | Yes | Yes |
| DJI Neo 2 special destination | Yes, USB ID `2ca3:0020` | Yes |
| SHA-256 verify, duplicate skip | Yes | Yes |
| SSD1306 OLED | Yes | Yes, with updated completion handling |
| Optional SMB sync | Yes, opt-in | Disabled |
| Internal Pi microSD detection | **Not in published importer** | Added |
| Ignore `.LRF` previews | **Not in published importer** | Added |
| Separate backup NVMe | **Not in published installer** | Added |
| Away-from-home idle poweroff | **Not in published installer** | Added, first live field test pending |

These differences matter: the code and manual must be reconciled and tested on a clean Pi before calling the repository a reproducible release of the latest build.

## 4. Before installation

1. Use a spare Raspberry Pi / test installation, **not** the author's live backup unit.
2. Install a supported 64-bit Raspberry Pi OS image with its root filesystem on NVMe. The installer explicitly rejects non-NVMe root storage.
3. Confirm you have working network access, adequate power and sufficient free disk space.
4. For OLED, enable I²C and wire the SSD1306 according to its voltage and pin requirements; check `i2cdetect -y 1` for address `3c`.
5. Back up existing configuration and data before running third-party scripts. Read `install.sh` and `payload/` before executing.

**Do not run the public installer over an existing FieldBackup installation.** It deliberately refuses to overwrite one.

## 5. Install the currently published beta

```bash
git clone https://github.com/Cooper6100/Fieldbackup-Pi.git
cd Fieldbackup-Pi
chmod +x install.sh
sudo ./install.sh
```

The installer prompts for confirmation, installs Debian packages, enables I²C if necessary, places services under `/opt` and `/etc/systemd/system`, and enables the importer and OLED services. It creates `/srv/sd-backups` on the **same filesystem as the NVMe OS** and records the root UUID.

Reboot if I²C was newly enabled:

```bash
sudo reboot
```

Inspect service state and live logs:

```bash
systemctl status sd-field-backup.service fieldbackup-oled.service
sudo journalctl -u sd-field-backup.service -f
sudo i2cdetect -y 1
findmnt -T /srv/sd-backups -o SOURCE,UUID,TARGET
```

The published installer is **not yet validated as a clean-Pi one-command deployment**. Package availability, device automounting and OLED wiring can vary.

## 6. Daily operation

1. Power on the Pi and wait for the importer service.
2. Insert a USB-connected SD/microSD card or attach a supported USB mass-storage camera with a `DCIM` folder.
3. Watch the OLED or journal for `Starting DCIM backup`, per-file `COPIED`/`SKIPPED`, and `Verified backup of`.
4. For volumes mounted by the importer, wait for `Safely unmounted` before removal. For media mounted by the operating system, verification **does not** mean that the OS has unmounted the card; safely unmount it or power down cleanly first.
5. Keep the original card until you have inspected the backup and made another independent copy.

The published importer supports USB FAT/exFAT volumes. The newer author's Pi also handles the internal microSD reader, but that support is **not yet in the public code**.

### Destination layout

The published code uses:

```text
/srv/sd-backups/
  YYYY-MM-DD/
    <volume-UUID>/
      ...contents of DCIM...
  DJI-Neo-2/
    ...contents of DCIM...
```

The Neo 2 destination is fixed so later connections can skip identical footage. Ordinary cards are grouped by **date and volume**, so the same card connected on a different day may create another copy. This is not a global content-addressed deduplication system.

### Copy and verification

The importer hashes the source, writes a `.partial` file, flushes it, hashes the temporary copy, and atomically renames it into place. If a destination file already exists and matches the source hash, it is skipped. A conflicting destination is treated as an error rather than overwritten. A minimum 15 GiB free-space margin is enforced in the published code.

### DJI and other cameras

The special `DJI-Neo-2` folder is selected by the device's USB vendor/product ID. Other devices exposing compatible mass storage and a `DCIM` directory can be ingested. DJI Osmo Nano footage has been tested on the author's newer Pi. MTP/PTP-only devices will not necessarily work: the camera must expose a compatible filesystem/block device.

## 7. OLED status

The SSD1306 display shows copy activity, filenames/counts, completion/error states and free space. **Note:** the published OLED code waits for a `Safely unmounted` event before displaying completion. The author's newer code was adjusted to handle already-mounted internal microSD cards; that fix is not yet included in the repository. Never interpret a display message as permission to unplug a still-mounted volume.

## 8. Optional SMB synchronization

The repository includes `configure-smb.sh` and a systemd sync timer. Sync is **optional** and should remain disabled until a share, credentials and destination semantics are configured and tested.

```bash
sudo ./configure-smb.sh
systemctl status fieldbackup-sync.timer
```

The author's current field unit has SMB sync disabled. The sync script's temporary-file and hard-link operations may not work on all SMB servers. Do not publish SMB credentials or share passwords.

## 9. Automatic shutdown — author's newer feature

The author's current unit has a separate watchdog service, `fieldbackup-shutdown.service`. It watches successful verified backups, the transfer lock, the expected destination mount, and home/away network state. After at least **180 seconds** idle and away from home, it can request a clean system poweroff.

**This watchdog is not yet distributed or installed by this repository.** Do not enable poweroff based solely on the documentation. The current version has passed dry-run checks, but its first real field shutdown is still to be validated.

**Known limitation:** A disconnected home Wi-Fi connection can be indistinguishable from being away; a long home-router outage could be interpreted as away. Journal polling also leaves a narrow race with newly attached media. The watchdog should be hardened and tested before a public release. For unattended use, prefer fail-safe behavior, a clear user override and a supervised first field test.

On the author's Pi, disabling the optional watchdog does not disable backups:

```bash
sudo systemctl disable --now fieldbackup-shutdown.service
```

This command only applies where the watchdog has been installed.

## 10. Troubleshooting

| Symptom | Check |
| --- | --- |
| No backup starts | `systemctl status sd-field-backup.service`; `lsblk -o NAME,TRAN,FSTYPE,MOUNTPOINT`; check for `DCIM` |
| Destination rejected | `findmnt -T /srv/sd-backups`; compare UUID to `/etc/sd-field-backup.json` |
| OLED blank | `i2cdetect -y 1`; `journalctl -u fieldbackup-oled.service -n 50`; wiring |
| Backup conflict | Inspect destination and source manually; do not delete files blindly |
| Low space | `df -h`; allow at least 15 GiB headroom |
| Already mounted read-write | Safely unmount the source and reconnect; importer intentionally refuses RW source |
| Import error | `sudo journalctl -u sd-field-backup.service -n 100 --no-pager` |
| SMB sync error | Keep sync disabled; inspect `fieldbackup-sync.service` logs and destination capabilities |

## 11. Safety and privacy

- Do not remove media or disconnect power during copying.
- Keep at least two independent copies before formatting a camera card.
- Verify backups independently with sample playback and hashes.
- Never commit `/etc/sd-field-backup.json` from a real machine, SMB credentials, Wi-Fi identifiers, passwords, home-router MACs, personal logs or private footage.
- Only install updates after reviewing changes; a backup appliance should fail safely.
- Test hardware compatibility and power loss behavior with expendable footage.

## 12. Project layout

```text
install.sh
configure-smb.sh
payload/
  sd-field-backup/backup.py
  fieldbackup-oled/display.py
  fieldbackup-sync/sync.sh
  sd-field-backup.json
  *.service / *.timer
README.md
LICENSE
docs/MANUAL.md
```

## 13. Roadmap / contributions

Priority work: reconcile the public installer with the separate-NVMe design; publish internal microSD and `.LRF` improvements; include and harden the shutdown watchdog; clean-install tests; improve OLED state handling; document Argon NEO 5 M.2 wiring and dual-NVMe compatibility; optionally add battery monitoring, progress estimates and a simple configuration UI.

Issues and pull requests are welcome. Please state your Pi model, OS version, source device type, mount state and relevant **redacted** logs.

**License:** MIT. See the repository `LICENSE`.
