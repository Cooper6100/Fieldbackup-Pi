# FieldBackup Pi v0.2.0 manual

## Target and prerequisites

The reference platform is a Raspberry Pi 5 with 16 GB RAM, Debian 13 Trixie ARM64, a 512 GB OS NVMe, a separate 4 TB backup NVMe and a working RTC battery. The SSD1306 OLED defaults to 128×64 on I²C bus 1 at address `0x3C`. Enable I²C using the board's boot configuration and confirm the device with `sudo i2cdetect -y 1`. Native PWM fan control is managed by the Pi firmware and kernel.

Install the required utilities and the packages for the features you use.

```bash
sudo apt install python3 util-linux
sudo apt install python3-luma.oled python3-pil i2c-tools        # OLED
sudo apt install python3-gpiozero python3-lgpio                 # LEDs
sudo apt install cifs-utils iproute2 iputils-ping               # SMB
```

Use NetworkManager if you intend to identify the home or away networks by connection profile. The optional GPIO LEDs default to BCM 17 (blue), 27 (green) and 22 (red).

## Backup SSD

Use a dedicated local filesystem with hard links and directory fsync, such as ext4. Mount it at `/mnt/media` before installation. Example `/etc/fstab` entry for the reference hardware:

```fstab
UUID=96caa633-c070-4529-b82b-57e314fbf95b /mnt/media ext4 defaults,nofail 0 2
```

Inspect the actual filesystem and device with `lsblk -f` and `findmnt -T /mnt/media -o SOURCE,TARGET,UUID,OPTIONS`. The importer requires the configured UUID at the exact mountpoint, writable, with the backup directory on that mount and the root filesystem on a different physical disk. The default requires both disks to be NVMe. Direct block filesystems are supported. If the SSD is absent or replaced during a backup, checked directory handles keep writes off the OS filesystem. If mounted late, the importer retries.

## Install, upgrade and rollback

1. Verify the release archive with `sha256sum -c FieldBackup-v0.2.0.tar.gz.sha256` and extract it. For a fresh install using different hardware settings, edit `payload/sd-field-backup.json` in the extracted package before applying. Set `oled.enabled` to `false` for a headless install.
2. Run `./install.sh`, `./upgrade.sh` or `./rollback.sh` for a preview. Run the corresponding command with `sudo` and `--apply` to change the host. Prefer a maintenance window for upgrades and rollbacks because they stop active transfers.
3. Fresh installation creates the backup directory only after validating the disk, installs the systemd units and starts the importer and configured OLED. Mounts, boot settings and dependencies are prepared by the operator.
4. Upgrade takes a snapshot of managed installation paths, merges existing JSON settings with defaults and migrates the legacy `root_uuid` key. It restores previously running importer, OLED and timer units. SMB credentials, network configuration, and backup data stay in their existing locations. The shutdown watchdog is disabled on upgrade and needs a fresh dry run before reactivation.
5. Rollback restores the prior managed paths and service states from `/var/lib/fieldbackup-install/previous`. After a fresh install it removes the managed program files and units. Data created by v0.2.0 stays available on the SSD. Earlier importer versions use the earlier folder layout. The previous snapshot can be archived by an administrator to permit another upgrade.

For a failed install or upgrade the script attempts to restore the snapshot. If a crash interrupts the operation after the snapshot has been published, use `rollback.sh --apply` with the released package. Machine-wide systemd customizations outside the managed units, such as overrides, are the operator's responsibility.

## Import behavior and recovery

- Insert a FAT/exFAT USB or SD volume. The importer mounts it read-only with `nosuid,nodev,noexec`, or checks that an existing mount is read-only and belongs to that partition. Existing mounts remain operator-managed. Enable `restrict_readers` to require a USB vendor/product ID listed in `reader_ids`.
- Files go under `Cards/<volume-uuid>/`, preserving paths from the root of the card. Dot-prefixed files and directories and non-regular files are skipped. Empty directories are omitted. Cards with cloned or identical volume UUIDs share a folder. Filename conflicts in that folder preserve both versions.
- An identical file at the same path is skipped after SHA-256 comparison. Different content is written alongside it as `conflict-<name-hash>-<sha256>.<extension>`. If a parent directory is occupied by a file, symlink or nested mount, the import stops with an error. Existing copies keep their timestamps. New files preserve source modification and access times to the precision supported by the filesystems. Creation times are filesystem-specific and are not copied.
- The importer hashes the source, writes and flushes a staged file, independently reads and verifies that file, flushes metadata, and publishes via a hard link that fails if the name is occupied. The backup tree contains only published files. The default space reserve is 15 GiB.
- After interruption, reinsert the card or let the importer retry with the card still connected. It removes stale files in its reserved `.fieldbackup-staging` directory and verifies already published files before skipping them. An interrupted file restarts from byte zero. The default retry delay for errors is 30 seconds. Files are recoverable across calendar dates because their destination is based on the volume UUID.
- v0.2.0 leaves previous date-stamped and DJI folders in place. The first reimport of a card can create a second local copy under the new layout.

## RTC and progress

The importer requires a plausible system clock and either synchronized NTP or a readable RTC within the configured drift threshold. The defaults are 2026-01-01 UTC, `rtc0`, and 120 seconds. Set `clock.minimum_epoch`, `clock.rtc_device` and `clock.max_rtc_drift_seconds` in `/etc/sd-field-backup.json` as appropriate. Check `timedatectl status`, `cat /sys/class/rtc/rtc0/since_epoch` and the timezone before field use. The validation cannot detect an RTC and unsynchronized system clock that agree on the same incorrect but plausible date.

The OLED shows storage and clock checks, scan state, current filename, verified file count, completion totals and SMB status when idle. Its settings are in the `oled` object. Green indicates verified completion. If a card was mounted externally, unmount it through its managing service before removal.

## Optional SMB sync and shutdown

Copy `payload/fieldbackup-network.example.json` to `/etc/fieldbackup-network.json`. Set the home NetworkManager connection profile and router IP/MAC, then run `sudo ./configure-smb.sh` to store credentials and enable the timer. Home profile and router checks are network heuristics. Use a trusted SMB server and restrict write access to the destination subfolder.

SMB uses its own mount namespace and lock. It reads committed files while imports continue. Files published after a scan passes their directory are picked up on the next run. The timer runs five minutes after the previous sync becomes inactive.

SMB transfers flush staging data and verify it with SHA-256. Linux CIFS publishes the verified file with an atomic no-replace rename and the sync confirms the final name before updating the manifest. A different existing file is kept, with the local content stored under a deterministic conflict name. If that conflict name is occupied by different data, sync logs an error and preserves both existing files. A share that rejects no-replace rename causes a sync error. SMB hard links are optional and unused. Check atomic rename, file fsync and timestamp precision on the actual NAS.

The local manifest at `/var/lib/fieldbackup-smb/verified.sqlite3` is disposable. It scopes cached verification to the configured local volume, source directory and SMB target. Sync skips hashing when source and destination fingerprints still match the verified record. The fingerprints include file identity, mode, size, modification time and change time. Missing or invalid entries and changes on either side trigger revalidation. To force a full recheck, stop the timer and sync service, remove the manifest and any associated SQLite journal files, then start the timer. Filesystems that present changed data with identical fingerprints can evade the fast path. A forced recheck verifies contents.

Each Pi uses a reserved, machine-specific hidden staging directory on the share. After a disconnect, the next run removes abandoned partials in its own directory and revalidates any final files already published. Use unique machine IDs on cloned Pis. Restrict write access to the share and staging directory to trusted accounts, and use a trusted NAS. Older `.partial` source files outside `Cards/` are excluded without deletion. SMB path components with stream syntax, device names or trailing spaces and dots cause sync to stop with an error; the original files remain on the SSD.

The shutdown watchdog is optional and ships in dry-run mode. For a controlled test, configure approved away profiles, run `sudo systemctl enable --now fieldbackup-shutdown.service`, and complete a new backup on an approved away connection. It requires 180 seconds idle, a successful new backup, a valid SSD, a free import lock, and an inactive SMB service. Confirm the dry-run message in the journal before considering poweroff. UPS telemetry and automatic low-battery shutdown are outside this release.

## Troubleshooting and validation

- Importer and card removal: `journalctl -u sd-field-backup.service -f`
- Storage: `findmnt -T /mnt/media/SD-Backups -o SOURCE,UUID,TARGET,OPTIONS`
- OLED: `journalctl -u fieldbackup-oled.service -n 50`
- SMB: `systemctl status fieldbackup-sync.timer` and `journalctl -u fieldbackup-sync.service -n 50`
- Shutdown: `journalctl -u fieldbackup-shutdown.service -n 50`

The automated suite exercises mount identity checks, clock validation, copy integrity, conflicts, recovery and SMB traversal with temporary directories and mocked device boundaries. Qualify the actual Pi, dual NVMe setup, offline RTC, OLED, reader and Samba/NAS server with expendable media before relying on them.
