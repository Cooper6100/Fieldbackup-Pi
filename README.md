# FieldBackup Pi v0.2.0

FieldBackup automatically copies FAT/exFAT SD cards and USB camera storage to a dedicated backup NVMe. It verifies each file with SHA-256 before publication, preserves file timestamps, and keeps both copies when filenames collide. The importer scans visible files across the whole card, including folders outside `DCIM`.

The target is a Raspberry Pi 5 (16 GB) running Debian 13 Trixie ARM64, with a 512 GB OS NVMe and a separate 4 TB backup NVMe. The default destination is `/mnt/media/SD-Backups`. The SSD UUID, mount, OLED, RTC, reader filtering, disk reserve and GPIO settings are configurable.

## Install, upgrade or roll back

Read the [operating manual](docs/MANUAL.md) for the mount, packages, RTC and OLED prerequisites. Run the script without arguments to preview its managed paths. Add `--apply` on a staging or target machine when ready.

```bash
./install.sh             # preview a fresh installation
./upgrade.sh             # preview an existing installation upgrade
./rollback.sh            # preview restoring the previous installation
```

New imports go under `Cards/<volume-uuid>/` inside the configured destination. The upgrade keeps the existing backups and preserves your configured destination and UUID. Optional SMB sync independently copies published files, including earlier backup layouts, and caches SHA-256 verification for unchanged files. It uses atomic no-replace rename on the SMB share. Keep the original media until you have checked an independent copy.

## Tests and package

```bash
python3 -m unittest discover -s tests -v
./scripts/package.sh /tmp/fieldbackup-package
```

The package script archives the current Git commit and produces a SHA-256 checksum. The pull request workflow builds the package and uploads it as a GitHub Actions artifact.

[MIT License](LICENSE)
