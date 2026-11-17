#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE="$(python3 -c 'import json; print(json.load(open("/etc/sd-field-backup.json"))["destination"])')"
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
NETWORK_CONFIG="/etc/fieldbackup-network.json"

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

# Fail closed: SMB sync only on explicitly configured home network.
[[ -f "$NETWORK_CONFIG" ]] || { log "Home network not configured; skipping"; exit 0; }
HOME_SSID="$(python3 -c 'import json; print(json.load(open("/etc/fieldbackup-network.json"))["home_ssid"])')"
HOME_IP="$(python3 -c 'import json; print(json.load(open("/etc/fieldbackup-network.json"))["home_router_ip"])')"
HOME_MAC="$(python3 -c 'import json; print(json.load(open("/etc/fieldbackup-network.json"))["home_router_mac"].lower())')"
HOME=0
if command -v nmcli >/dev/null 2>&1; then
    CONNECTION="$(nmcli -g GENERAL.CONNECTION device show wlan0 2>/dev/null || true)"
    STATE="$(nmcli -g GENERAL.STATE device show wlan0 2>/dev/null || true)"
    [[ "$CONNECTION" == "$HOME_SSID" && "$STATE" == 100* ]] && HOME=1
fi
if (( ! HOME )); then
    ping -c 1 -W 1 "$HOME_IP" >/dev/null 2>&1 || true
    ROUTER_MAC="$(ip -4 neigh show "$HOME_IP" | awk '{print tolower($5)}')"
    [[ -n "$HOME_MAC" && "$ROUTER_MAC" == "$HOME_MAC" ]] && HOME=1
fi
(( HOME )) || { log "Not on home network; skipping"; exit 0; }

EXPECTED_UUID="$(python3 -c 'import json; print(json.load(open("/etc/sd-field-backup.json"))["root_uuid"])')"
ACTUAL_UUID="$(findmnt -n -o UUID -T "$SOURCE" || true)"
[[ "$ACTUAL_UUID" == "$EXPECTED_UUID" ]] || { log "Backup NVMe missing or incorrect"; exit 1; }

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
