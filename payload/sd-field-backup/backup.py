#!/usr/bin/env python3
"""Read-only FAT/exFAT media importer."""
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from storage import (
    STAGING, backup_volume, cleanup_staging, command, configuration,
    copy_verified, directory, mount_id, mount_info, pinned, scan_files,
)

LOCK = "/run/lock/fieldbackup-transfer.lock"
RUNTIME = Path("/run/sd-field-backup")
STOP = False
log = logging.getLogger("sd-field-backup")


def stop(*_):
    global STOP
    STOP = True


def check_stop():
    if STOP:
        raise InterruptedError("Importer stopping")


def status(state, message="", **progress):
    record = dict(state=state, message=message, updated=time.monotonic(), **progress)
    temporary = RUNTIME / "status.tmp"
    temporary.write_text(json.dumps(record))
    temporary.replace(RUNTIME / "status.json")


def clock_source(cfg):
    settings = cfg.get("clock", {})
    now = time.time()
    if now < settings.get("minimum_epoch", 1767225600):
        raise RuntimeError("System date is too old")
    try:
        if command("timedatectl", "show", "-p", "NTPSynchronized", "--value") == "yes":
            return "NTP"
    except (subprocess.SubprocessError, OSError):
        pass
    rtc = settings.get("rtc_device", "rtc0")
    if not re.fullmatch(r"rtc[0-9]+", rtc):
        raise ValueError("Invalid RTC device name")
    try:
        rtc_time = int((Path("/sys/class/rtc") / rtc / "since_epoch").read_text())
    except (OSError, ValueError):
        raise RuntimeError("RTC unavailable and NTP unsynchronized") from None
    if rtc_time < settings.get("minimum_epoch", 1767225600):
        raise RuntimeError("RTC date is too old")
    if abs(rtc_time - now) > settings.get("max_rtc_drift_seconds", 120):
        raise RuntimeError("System clock and RTC disagree")
    return rtc


def usb_id(dev):
    node = (Path("/sys/class/block") / Path(dev).name).resolve()
    for parent in (node, *node.parents):
        try:
            vendor = (parent / "idVendor").read_text().strip().lower()
            product = (parent / "idProduct").read_text().strip().lower()
            return f"{vendor}:{product}"
        except FileNotFoundError:
            continue
    return None


def partitions(cfg):
    data = json.loads(command("lsblk", "-J", "-p", "-o",
                              "NAME,TYPE,FSTYPE,UUID,TRAN,MAJ:MIN,MOUNTPOINTS"))
    allowed = set(cfg.get("reader_ids", []))
    result = {}

    def visit(node, disk=None):
        if node["type"] == "disk":
            disk = node
        if (disk and (disk["tran"] in ("usb", "mmc") or Path(disk["name"]).name.startswith("mmcblk"))
                and node["type"] in ("part", "disk")
                and node["fstype"] in ("exfat", "vfat", "fat", "fat32")):
            dev = node["name"]
            reader = usb_id(dev)
            if not cfg.get("restrict_readers", False) or reader in allowed:
                result[dev] = node | {"reader": reader}
        for child in node.get("children", []):
            visit(child, disk)

    for node in data["blockdevices"]:
        visit(node)
    return result


@contextmanager
def source_volume(dev, part):
    existing = [path for path in (part.get("mountpoints") or []) if path]
    owned = not existing
    if owned:
        source = RUNTIME / Path(dev).name
        source.mkdir(exist_ok=True)
        command("mount", "-o", "ro,nosuid,nodev,noexec", dev, str(source))
    else:
        if len(existing) != 1:
            raise RuntimeError(f"Ambiguous source mounts for {dev}")
        source = Path(existing[0])
    try:
        with directory(source) as fd:
            info = mount_info(source)
            if (info["target"] != str(source) or info["fsroot"] != "/"
                    or int(info["id"]) != mount_id(fd) or info["maj:min"] != part["maj:min"]
                    or (info["uuid"] or "").lower() != (part.get("uuid") or "").lower()
                    or "ro" not in info["options"].split(",")):
                raise RuntimeError(f"Expected read-only source mount for {dev}")
            yield fd
    finally:
        if owned:
            try:
                command("umount", str(source))
            except (OSError, subprocess.SubprocessError):
                log.exception("Could not unmount %s", dev)
                raise


def card_folder(part):
    uuid = part.get("uuid")
    if not uuid or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", uuid):
        raise RuntimeError("Source volume has no usable filesystem UUID")
    return Path("Cards") / uuid.lower()


def import_card(dev, part, cfg, lights):
    status("CHECKING", "Clock and storage")
    try:
        clock = clock_source(cfg)
    except RuntimeError as error:
        status("CLOCK ERROR", str(error))
        raise
    reserve = int(cfg.get("reserve_gib", 15) * 1024**3)
    if reserve < 0:
        raise ValueError("Storage reserve cannot be negative")
    with backup_volume(cfg) as dest, source_volume(dev, part) as card:
        status("SCANNING", Path(dev).name)
        root = pinned(card)
        files = []
        for src in scan_files(root):
            check_stop()
            files.append(src.relative_to(root))
        if not files:
            raise RuntimeError("No visible files on media")
        folder = card_folder(part)
        log.info("Starting media backup %s -> %s (clock: %s)", dev, folder, clock)
        lights.show("copying")
        copied = skipped = conflicts = 0
        with directory(STAGING, below=dest, create=True) as stage:
            cleanup_staging(stage)
            for index, rel in enumerate(files):
                check_stop()
                status("COPY / VERIFY", rel.name, done=index, total=len(files))
                with directory(rel.parent, below=card) as source_parent:
                    with directory(folder / rel.parent, below=dest, create=True) as parent:
                        outcome = copy_verified(pinned(source_parent) / rel.name, parent,
                                                rel.name, stage, reserve=reserve, stop=check_stop,
                                                source_parent=source_parent)
                copied += outcome != "SKIPPED"
                skipped += outcome == "SKIPPED"
                conflicts += outcome == "CONFLICT"
                log.info("%s %r", outcome, str(rel))
        # A pinned path cannot fall through to the OS disk. Also confirm that this
        # is still the visible destination before reporting verified completion.
        with backup_volume(cfg) as current:
            if mount_id(current) != mount_id(dest):
                raise RuntimeError("Backup mount changed during import")
        status("FINALIZING", "Closing source", done=len(files), total=len(files))
        log.info("Verified backup of %s: %d copied, %d identical, %d conflicts",
                 dev, copied, skipped, conflicts)
    log.info("Backup completed safely for %s", dev)
    status("COMPLETE", "Verified; unmount RO" if any(part.get("mountpoints") or [])
           else "Verified; remove card", done=len(files), total=len(files),
           copied=copied, skipped=skipped, conflicts=conflicts)
    lights.show("green")


class Lights:
    def __init__(self, cfg):
        self.leds = {}
        try:
            from gpiozero import LED
            for color, pin in cfg.get("gpio_pins", {}).items():
                self.leds[color] = LED(int(pin))
        except Exception as error:
            log.warning("GPIO LEDs unavailable: %s", error)
            self.close()
            self.leds.clear()

    def show(self, state):
        for led in self.leds.values():
            led.off()
        if state == "copying" and "blue" in self.leds:
            self.leds["blue"].blink(on_time=.3, off_time=.3, background=True)
        elif state in self.leds:
            self.leds[state].on()

    def close(self):
        for led in self.leds.values():
            led.close()


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = configuration()
    lights = Lights(cfg)
    completed = set()
    retry = {}
    status("STARTING")
    idle = None
    try:
        while not STOP:
            try:
                found = partitions(cfg)
                active = {(dev, part.get("uuid"), part["maj:min"]) for dev, part in found.items()}
                completed &= active
                retry = {key: due for key, due in retry.items() if key in active}
                if not active:
                    try:
                        with backup_volume(cfg) as dest:
                            free = os.fstatvfs(dest)
                        ready = ("READY", f"{free.f_bavail * free.f_frsize // 1024**3} GiB free")
                    except (RuntimeError, OSError, subprocess.SubprocessError):
                        ready = ("STORAGE ERROR", "Check backup SSD")
                    if ready[0] == "READY":
                        try:
                            clock_source(cfg)
                        except RuntimeError:
                            ready = ("CLOCK ERROR", "Check RTC / NTP")
                    if idle != ready:
                        status(*ready)
                        lights.show("ready" if ready[0] == "READY" else "red")
                        idle = ready
                else:
                    idle = None
                for key in sorted(active - completed, key=lambda item: item[0]):
                    if STOP or retry.get(key, 0) > time.monotonic():
                        continue
                    dev = key[0]
                    try:
                        with open(LOCK, "a") as lock:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                continue
                            import_card(dev, found[dev], cfg, lights)
                        completed.add(key)
                    except Exception as error:
                        log.exception("Backup failed for %s", dev)
                        current = json.loads((RUNTIME / "status.json").read_text())
                        if current["state"] != "CLOCK ERROR":
                            status("BACKUP ERROR", str(error))
                        lights.show("red")
                        retry[key] = time.monotonic() + cfg.get("retry_seconds", 30)
            except Exception:
                log.exception("Detection error")
                status("READER ERROR", "Check backup logs")
                idle = None
                lights.show("red")
            if not STOP:
                time.sleep(3)
    finally:
        lights.close()


if __name__ == "__main__":
    main()
