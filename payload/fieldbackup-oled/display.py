#!/usr/bin/env python3

import json
import re
import select
import shutil
import subprocess
import time
from pathlib import Path

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306
from PIL import ImageFont

CONFIG = Path("/etc/sd-field-backup.json")
DEST = Path(json.loads(CONFIG.read_text())["destination"])

serial = i2c(port=1, address=0x3C)
device = ssd1306(serial, width=128, height=64)
font = ImageFont.load_default()

state = "READY"
filename = ""
processed = 0
copied = 0
skipped = 0
message = "Insert SD card"

verified = False
completed_at = None
last_error = False

sync_state = None
sync_message = ""
sync_completed_at = None


def shorten(value, limit=18):
    if len(value) <= limit:
        return value
    return "..." + value[-(limit - 3):]


def draw_screen():
    try:
        usage = shutil.disk_usage(DEST)
        storage = f"FREE: {usage.free / (1024**3):.0f} GiB"
    except OSError:
        storage = "STORAGE ERROR"

    with canvas(device) as draw:
        draw.text((0, 0), "FIELDBACKUP", font=font, fill="white")
        draw.line((0, 13, 127, 13), fill="white")

        sd_priority = state in ("COPYING", "VERIFYING", "ERROR", "COMPLETE")
        shown_state = state if sd_priority else (sync_state or state)

        draw.text((0, 16), shown_state, font=font, fill="white")

        if shown_state == "HOME SYNC":
            draw.text((0, 29), shorten(sync_message), font=font, fill="white")

        elif shown_state == "SYNC COMPLETE":
            draw.text((0, 29), "Home backup done", font=font, fill="white")

        elif shown_state == "SYNC ERROR":
            draw.text((0, 29), "Check sync logs", font=font, fill="white")

        elif state == "COPYING":
            draw.text((0, 29), shorten(filename), font=font, fill="white")
            draw.text((0, 41), f"FILES: {processed}", font=font, fill="white")

        elif state == "COMPLETE":
            draw.text((0, 29), "Safe to remove SD", font=font, fill="white")
            draw.text(
                (0, 41),
                f"{copied} NEW / {skipped} SAME",
                font=font,
                fill="white"
            )

        else:
            draw.text((0, 29), shorten(message), font=font, fill="white")

        draw.line((0, 52, 127, 52), fill="white")
        draw.text((0, 54), storage, font=font, fill="white")


def handle_log(line):
    global state, filename, processed, copied, skipped
    global message, verified, completed_at, last_error

    if "Starting DCIM backup" in line:
        state = "COPYING"
        filename = "Starting..."
        processed = copied = skipped = 0
        verified = False
        completed_at = None
        last_error = False
        message = "Copying files"

    elif "Could not unmount" in line:
        state = "ERROR"
        message = "DO NOT REMOVE SD"
        last_error = True
        completed_at = None

    elif "Backup failed for" in line or "Detection error:" in line:
        state = "ERROR"
        message = "Check backup logs"
        last_error = True
        completed_at = None

    elif "Verified backup of" in line:
        verified = True
        if not last_error:
            state = "VERIFYING"
            message = "Finalizing backup"

    elif "Safely unmounted" in line:
        if verified and not last_error:
            state = "COMPLETE"
            message = "Safe to remove SD"
            completed_at = time.monotonic()
        # Unmounting without verification is not success.
        # Wait for the actual failure log.

    elif "COPIED " in line or "SKIPPED " in line:
        match = re.search(r"\b(COPIED|SKIPPED)\s+(.+)$", line)
        if match and not last_error:
            status, filename = match.groups()
            processed += 1
            if status == "COPIED":
                copied += 1
            else:
                skipped += 1
            state = "COPYING"



def handle_sync_log(line):
    global sync_state, sync_message, sync_completed_at

    if "FieldBackup Sync:" not in line:
        return

    if "Starting incremental synchronization" in line:
        sync_state = "HOME SYNC"
        sync_message = "Checking files"
        sync_completed_at = None

    elif "COPYING " in line:
        sync_state = "HOME SYNC"
        sync_message = line.split("COPYING ", 1)[1].strip()

    elif "VERIFIED " in line:
        sync_state = "HOME SYNC"
        sync_message = "File verified"

    elif "Synchronization completed successfully" in line:
        sync_state = "SYNC COMPLETE"
        sync_message = "Home backup done"
        sync_completed_at = time.monotonic()

    elif any(x in line for x in (
        "CONFLICT:", "Verification failed", "Unsafe destination",
        "ERROR:", "refusing to proceed"
    )):
        sync_state = "SYNC ERROR"
        sync_message = "Check sync logs"
        sync_completed_at = None


def reset_sync_if_complete():
    global sync_state, sync_message, sync_completed_at

    if (
        sync_state == "SYNC COMPLETE"
        and sync_completed_at is not None
        and time.monotonic() - sync_completed_at >= 15
    ):
        sync_state = None
        sync_message = ""
        sync_completed_at = None


def reset_if_complete():
    global state, filename, processed, copied, skipped
    global message, verified, completed_at

    if (
        state == "COMPLETE"
        and completed_at is not None
        and time.monotonic() - completed_at >= 15
    ):
        state = "READY"
        message = "Insert SD card"
        filename = ""
        processed = copied = skipped = 0
        verified = False
        completed_at = None


def main():
    draw_screen()

    process = subprocess.Popen(
        [
            "journalctl",
            "-u", "sd-field-backup.service",
            "-u", "fieldbackup-sync.service",
            "-f",
            "-n", "0",
            "-o", "cat",
            "--no-pager"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=False,
        bufsize=0
    )

    try:
        while True:
            readable, _, _ = select.select(
                [process.stdout], [], [], 1
            )

            if readable:
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("Journal stream ended")
                decoded = line.decode("utf-8", errors="replace")
                if "FieldBackup Sync:" in decoded:
                    handle_sync_log(decoded)
                else:
                    handle_log(decoded)

            reset_sync_if_complete()
            reset_if_complete()
            draw_screen()

    finally:
        process.terminate()


if __name__ == "__main__":
    main()
