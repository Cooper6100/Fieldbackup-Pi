#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE="/srv/sd-backups"
MOUNT="/mnt/home-smb"
CREDS="/etc/fieldbackup-smb.credentials"
CONFIG="/etc/fieldbackup-smb.conf"
[[ -f "$CONFIG" ]] || { echo "SMB sync not configured; run configure-smb.sh"; exit 1; }
# Config file is root-owned and written by configure-smb.sh.
source "$CONFIG"
: "${SERVER:?Missing SMB SERVER}"
: "${REMOTE_DIR:?Missing REMOTE_DIR}"
TARGET="$MOUNT/$REMOTE_DIR"
LOCK="/run/lock/fieldbackup-transfer.lock"

MOUNTED_BY_US=0

log() {
    echo "FieldBackup Sync: $*"
}

cleanup() {
    if (( MOUNTED_BY_US )); then
        umount "$MOUNT" || log "WARNING: Could not unmount SMB"
    fi
}

trap cleanup EXIT
trap 'log "ERROR: Synchronization failed (line $LINENO)"' ERR

SMB_HOST="${SERVER#//}"
SMB_HOST="${SMB_HOST%%/*}"
if ! timeout 5 bash -c 'exec 3<>/dev/tcp/"$1"/445' _ "$SMB_HOST" 2>/dev/null; then
    log "Home SMB server unavailable; retrying later"
    exit 0
fi

exec 9>"$LOCK"

if ! flock -n 9; then
    log "SD backup or another sync active; retrying later"
    exit 0
fi

if mountpoint -q "$MOUNT"; then
    log "ERROR: SMB mount already active; refusing to proceed"
    exit 1
fi

mkdir -p "$MOUNT"

mount -t cifs "$SERVER" "$MOUNT" \
    -o "credentials=$CREDS,vers=3.0,iocharset=utf8"

MOUNTED_BY_US=1

mkdir -p "$TARGET"

log "Starting incremental synchronization"

python3 - "$SOURCE" "$TARGET" <<'PYCODE'
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
manifest_path = Path("/var/lib/fieldbackup-sync/manifest.json")

manifest_path.parent.mkdir(parents=True, exist_ok=True)

try:
    manifest = json.loads(manifest_path.read_text())
except FileNotFoundError:
    manifest = {}

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def save_manifest():
    temp = manifest_path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2))
    os.replace(temp, manifest_path)

copied = 0
verified = 0
skipped = 0

for src in sorted(source.rglob("*")):
    if not src.is_file() or src.name.endswith(".partial"):
        continue

    relative = src.relative_to(source)
    key = relative.as_posix()
    dst = target / relative

    src_stat = src.stat()

    if dst.is_symlink():
        raise RuntimeError(f"CONFLICT: Destination symlink: {key}")

    entry = manifest.get(key)

    if dst.exists():
        if not dst.is_file():
            raise RuntimeError(f"CONFLICT: Unsafe destination: {key}")

        dst_stat = dst.stat()

        if (
            entry
            and entry["source_size"] == src_stat.st_size
            and entry["source_mtime_ns"] == src_stat.st_mtime_ns
            and entry["dest_size"] == dst_stat.st_size
            and entry["dest_mtime_ns"] == dst_stat.st_mtime_ns
        ):
            skipped += 1
            continue

        print(f"FieldBackup Sync: VERIFYING {key}", flush=True)

        source_hash = sha256(src)
        dest_hash = sha256(dst)

        if source_hash != dest_hash:
            raise RuntimeError(f"CONFLICT: Existing SMB file differs: {key}")

        manifest[key] = {
            "source_size": src_stat.st_size,
            "source_mtime_ns": src_stat.st_mtime_ns,
            "dest_size": dst_stat.st_size,
            "dest_mtime_ns": dst_stat.st_mtime_ns,
            "sha256": source_hash,
        }

        save_manifest()
        verified += 1
        continue

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".partial")

    if tmp.exists():
        tmp.unlink()

    print(f"FieldBackup Sync: COPYING {key}", flush=True)

    shutil.copyfile(src, tmp)

    source_hash = sha256(src)
    dest_hash = sha256(tmp)

    if source_hash != dest_hash:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Verification failed: {key}")

    os.link(tmp, dst)
    tmp.unlink()

    dst_stat = dst.stat()

    manifest[key] = {
        "source_size": src_stat.st_size,
        "source_mtime_ns": src_stat.st_mtime_ns,
        "dest_size": dst_stat.st_size,
        "dest_mtime_ns": dst_stat.st_mtime_ns,
        "sha256": source_hash,
    }

    save_manifest()
    copied += 1
    verified += 1

    print(f"FieldBackup Sync: VERIFIED {key}", flush=True)

print(
    f"FieldBackup Sync: Synchronization completed successfully "
    f"({copied} copied, {verified} verified, {skipped} unchanged)",
    flush=True,
)
PYCODE
