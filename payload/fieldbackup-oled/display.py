#!/usr/bin/env python3
"""SSD1306 display for atomic importer and SMB status snapshots."""
import json
import time
from pathlib import Path

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306
from PIL import ImageFont

CONFIG = Path("/etc/sd-field-backup.json")
IMPORT = Path("/run/sd-field-backup/status.json")
SYNC = Path("/run/fieldbackup-sync/status.json")


def read_status(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def visible_status(local, remote, now):
    state = local.get("state")
    if state == "COMPLETE" and now - local["updated"] >= 20:
        local = {"state": "READY", "message": "Insert media"}
    if local.get("state") == "READY" and remote:
        limit = 120 if remote["state"] == "HOME SYNC" else 20
        if now - remote["updated"] < limit:
            return remote
    return local or {"state": "WAITING", "message": "Importer starting"}


def draw_status(device, font, record):
    def line(draw, y, value):
        value = value.replace("\n", " ").replace("\r", " ")
        while value and draw.textlength(value, font=font) > device.width:
            value = value[:-1]
        draw.text((0, y), value, font=font, fill="white")

    with canvas(device) as draw:
        line(draw, 0, "FieldBackup 0.2.0")
        line(draw, 14, record["state"])
        line(draw, 28, record.get("message", ""))
        total = record.get("total", 0)
        if total:
            done = record.get("done", 0)
            if record["state"] == "COMPLETE":
                line(draw, 42, f'{record["copied"]} new / {record["skipped"]} same')
                if record["conflicts"]:
                    line(draw, 53, f'{record["conflicts"]} conflicts kept')
            else:
                line(draw, 42, f"{done}/{total} files")
                draw.rectangle((0, 55, device.width - 1, 62), outline="white")
                width = (device.width - 2) * done // total
                if width:
                    draw.rectangle((1, 56, width, 61), fill="white")


def main():
    settings = json.loads(CONFIG.read_text()).get("oled", {})
    if not settings.get("enabled", True):
        return
    address = settings.get("address", 0x3C)
    if isinstance(address, str):
        address = int(address, 0)
    serial = i2c(port=settings.get("bus", 1), address=address)
    device = ssd1306(serial, width=settings.get("width", 128), height=settings.get("height", 64))
    font = ImageFont.load_default()
    previous = None
    while True:
        record = visible_status(read_status(IMPORT), read_status(SYNC), time.monotonic())
        if record != previous:
            draw_status(device, font, record)
            previous = record
        time.sleep(0.5)


if __name__ == "__main__":
    main()
