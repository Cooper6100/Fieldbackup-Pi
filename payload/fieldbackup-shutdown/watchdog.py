#!/usr/bin/env python3
"""FieldBackup optional shutdown watchdog. Disabled until explicitly configured."""
import fcntl
import json
import logging
import re
import subprocess
import time
from pathlib import Path

CONFIG = Path("/etc/sd-field-backup.json")
NETWORK_CONFIG = Path("/etc/fieldbackup-network.json")
LOCK = Path("/run/lock/fieldbackup-transfer.lock")
IDLE_SECONDS = 180
POLL_SECONDS = 3
DRY_RUN = True  # Keep True until tested on your hardware.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fieldbackup-shutdown")

def run(args, timeout=10):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)

def destination_ok():
    try:
        cfg = json.loads(CONFIG.read_text())
        dest = Path(cfg["destination"])
        if not dest.is_dir() or dest.is_symlink():
            return False
        r = run(["findmnt", "-n", "-o", "UUID", "-T", str(dest)])
        return r.returncode == 0 and r.stdout.strip() == cfg["root_uuid"]
    except Exception:
        return False

def importer_ok():
    r = run(["systemctl", "is-active", "sd-field-backup.service"])
    return r.returncode == 0 and r.stdout.strip() == "active"

def network_state():
    try:
        cfg = json.loads(NETWORK_CONFIG.read_text())
        home_ssid = cfg["home_ssid"]
        home_ip = cfg["home_router_ip"]
        home_mac = cfg["home_router_mac"].lower()
        away_ssids = set(cfg.get("away_ssids", [])) - {home_ssid}
        r = run(["nmcli", "-t", "-f", "GENERAL.CONNECTION,GENERAL.STATE", "device", "show", "wlan0"])
        if r.returncode != 0:
            return "UNKNOWN"
        values = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
        connection = values.get("GENERAL.CONNECTION", "")
        connected = values.get("GENERAL.STATE", "").startswith("100 ")
        if connected and connection == home_ssid:
            return "HOME"
        route = run(["ip", "-4", "route", "get", home_ip])
        if route.returncode == 0:
            match = re.search(r"\bdev\s+(\S+)", route.stdout)
            if match:
                iface = match.group(1)
                run(["ping", "-c", "1", "-W", "1", home_ip])
                neigh = run(["ip", "-4", "neigh", "show", home_ip, "dev", iface])
                if home_mac and home_mac in neigh.stdout.lower():
                    return "HOME"
        if connected and connection in away_ssids:
            return "AWAY"
        return "UNKNOWN"
    except Exception:
        return "UNKNOWN"

def journal_start_cursor():
    r = run(["journalctl", "-u", "sd-field-backup.service", "-n", "1", "-o", "json", "--no-pager"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return json.loads(r.stdout.splitlines()[-1])["__CURSOR"]

def journal_events(cursor):
    r = run(["journalctl", "-u", "sd-field-backup.service", "--after-cursor", cursor, "-o", "json", "--no-pager"])
    if r.returncode != 0:
        raise RuntimeError("Journal read failed")
    events = []
    for line in r.stdout.splitlines():
        entry = json.loads(line)
        cursor = entry["__CURSOR"]
        events.append(entry.get("MESSAGE", ""))
    return events, cursor

def lock_available():
    with open(LOCK, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(f, fcntl.LOCK_UN)
        return True

def protected_shutdown():
    with open(LOCK, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Transfer active; shutdown cancelled")
            return False
        try:
            if not importer_ok() or not destination_ok():
                log.warning("Importer or destination unavailable")
                return False
            sync = run(["systemctl", "is-active", "fieldbackup-sync.service"])
            if sync.stdout.strip() != "inactive":
                log.info("SMB synchronization active or uncertain; shutdown cancelled")
                return False
            if network_state() != "AWAY":
                log.info("Home or unknown network; shutdown cancelled")
                return False
            if DRY_RUN:
                log.warning("DRY RUN: Would shut down now")
                return True
            log.warning("All checks passed; requesting poweroff")
            r = run(["systemctl", "poweroff"])
            return r.returncode == 0
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def main():
    cursor = journal_start_cursor()
    if cursor is None:
        raise RuntimeError("Cannot initialize journal cursor")
    pending = verified = completed = False
    completed_at = away_since = last_network = None
    log.info("Watchdog started; dry_run=%s", DRY_RUN)
    while True:
        try:
            events, cursor = journal_events(cursor)
            for line in events:
                if "Starting DCIM backup" in line:
                    pending = True
                    verified = completed = False
                    completed_at = away_since = None
                    log.info("Backup started; countdown cancelled")
                elif "Verified backup of" in line and pending:
                    verified = True
                    log.info("Backup verified; waiting for cleanup")
                elif "Backup completed safely for" in line and pending and verified:
                    completed = True
                    log.info("Backup completion confirmed")
                elif any(s in line for s in ("Backup failed for", "Could not unmount", "Detection error:")):
                    pending = verified = completed = False
                    completed_at = away_since = None
                    log.warning("Backup error; shutdown cancelled")
            if pending and verified and completed and lock_available():
                pending = verified = completed = False
                completed_at = time.monotonic()
                log.info("Cleanup complete; 180-second countdown started")
            state = network_state()
            if state != last_network:
                log.info("Network state: %s", state)
                last_network = state
            if state == "AWAY":
                if away_since is None:
                    away_since = time.monotonic()
            else:
                away_since = None
            if completed_at is not None:
                now = time.monotonic()
                if not lock_available():
                    completed_at = now
                    if away_since is not None:
                        away_since = now
                elif away_since is not None and now - completed_at >= IDLE_SECONDS and now - away_since >= IDLE_SECONDS:
                    protected_shutdown()
                    completed_at = away_since = None
        except Exception:
            log.exception("Watchdog error; countdown cancelled")
            pending = verified = completed = False
            completed_at = away_since = None
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
