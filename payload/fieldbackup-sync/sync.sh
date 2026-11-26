#!/usr/bin/env bash
set -Eeuo pipefail

MOUNT="/run/fieldbackup-sync/share"
CREDS="/etc/fieldbackup-smb.credentials"
CONFIG="/etc/fieldbackup-smb.conf"
NETWORK_CONFIG="/etc/fieldbackup-network.json"
TRANSFER="$(dirname "$0")/transfer.py"
MOUNTED_BY_US=0

log() {
    echo "FieldBackup Sync: $*"
}

cleanup() {
    local result=$?
    if (( MOUNTED_BY_US )); then
        timeout 20 umount "$MOUNT" || result=1
    fi
    if (( result )); then
        log "ERROR: Synchronization failed"
        python3 "$TRANSFER" --status "SYNC ERROR"
    elif (( MOUNTED_BY_US )); then
        python3 "$TRANSFER" --status "SYNC COMPLETE"
    fi
    exit "$result"
}

trap cleanup EXIT
[[ -f "$CONFIG" && -f "$CREDS" ]] || { log "SMB sync not configured; skipping"; exit 0; }
source "$CONFIG"
: "${SERVER:?Missing SMB SERVER}"
: "${REMOTE_DIR:?Missing REMOTE_DIR}"
[[ "$SERVER" =~ ^//[A-Za-z0-9._-]+/[A-Za-z0-9._$-]+$ ]] || exit 1
[[ "$REMOTE_DIR" =~ ^[A-Za-z0-9_-][A-Za-z0-9._-]*$ ]] || exit 1

# Fail closed: SMB sync only on explicitly configured home network.
[[ -f "$NETWORK_CONFIG" ]] || { log "Home network not configured; skipping"; exit 0; }
HOME_SSID="$(python3 -c 'import json; print(json.load(open("/etc/fieldbackup-network.json"))["home_ssid"])')"
HOME_IP="$(python3 -c 'import json; print(json.load(open("/etc/fieldbackup-network.json"))["home_router_ip"])')"
HOME_MAC="$(python3 -c 'import json; print(json.load(open("/etc/fieldbackup-network.json"))["home_router_mac"].lower())')"
ON_HOME_NETWORK=0
if command -v nmcli >/dev/null 2>&1; then
    CONNECTION="$(nmcli -g GENERAL.CONNECTION device show wlan0 2>/dev/null || true)"
    STATE="$(nmcli -g GENERAL.STATE device show wlan0 2>/dev/null || true)"
    [[ "$CONNECTION" == "$HOME_SSID" && "$STATE" == 100* ]] && ON_HOME_NETWORK=1
fi
if (( ! ON_HOME_NETWORK )); then
    ping -c 1 -W 1 "$HOME_IP" >/dev/null 2>&1 || true
    ROUTER_MAC="$(ip -4 neigh show "$HOME_IP" | awk '{print tolower($5)}')"
    [[ -n "$HOME_MAC" && "$ROUTER_MAC" == "$HOME_MAC" ]] && ON_HOME_NETWORK=1
fi
(( ON_HOME_NETWORK )) || { log "Not on home network; skipping"; exit 0; }

SMB_HOST="${SERVER#//}"
SMB_HOST="${SMB_HOST%%/*}"
if ! timeout 5 bash -c 'exec 3<>/dev/tcp/"$1"/445' _ "$SMB_HOST" 2>/dev/null; then
    log "Home SMB server unavailable; retrying later"
    exit 0
fi

exec 9>/run/lock/fieldbackup-smb.lock
flock -n 9 || exit 0

mkdir -p "$MOUNT"
if mountpoint -q "$MOUNT"; then
    log "ERROR: SMB mount already active"
    exit 1
fi
timeout 30 mount -t cifs "$SERVER" "$MOUNT" \
    -o "credentials=$CREDS,vers=3.0,iocharset=utf8,nosuid,nodev,noexec,soft,cache=none,actimeo=0,serverino"
MOUNTED_BY_US=1
python3 "$TRANSFER" "$MOUNT" "$REMOTE_DIR" "$SERVER"
