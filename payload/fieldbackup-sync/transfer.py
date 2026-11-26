"""Synchronize only complete, atomically published backup files."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sd-field-backup"))
from storage import (STAGING, backup_volume, cleanup_staging, configuration,
                     copy_verified, directory, mount_id, mount_info, pinned, scan_files, sync_directory)

RUNTIME = Path("/run/fieldbackup-sync")


def status(state, message):
    record = dict(state=state, message=message, updated=time.monotonic())
    temporary = RUNTIME / "status.tmp"
    temporary.write_text(json.dumps(record))
    temporary.replace(RUNTIME / "status.json")


def sync(mount, subfolder, server):
    if Path(subfolder).name != subfolder or subfolder.startswith("."):
        raise ValueError("Unsafe SMB subfolder")
    with backup_volume(configuration()) as source, directory(mount) as share:
        info = mount_info(mount)
        if (info["target"] != mount or int(info["id"]) != mount_id(share)
                or info["fstype"] not in ("cifs", "smb3") or info["source"] != server
                or info["fsroot"] != "/" or "rw" not in info["options"].split(",")):
            raise RuntimeError("Expected writable SMB mount")
        with directory(subfolder, below=share, create=True, remote=True) as target:
            with directory(STAGING, below=target, create=True, remote=True) as stage:
                cleanup_staging(stage, remote=True)
                root = pinned(source)
                totals = dict(COPIED=0, SKIPPED=0, CONFLICT=0)
                for src in scan_files(root):
                    rel = src.relative_to(root)
                    if rel.parts[0] != "Cards" and src.name.endswith(".partial"):
                        continue  # Legacy in-place staging convention
                    status("HOME SYNC", src.name)
                    with directory(rel.parent, below=source) as src_parent:
                        with directory(rel.parent, below=target, create=True, remote=True) as dst_parent:
                            result = copy_verified(pinned(src_parent) / rel.name, dst_parent,
                                                   rel.name, stage, remote=True, source_parent=src_parent)
                    totals[result] += 1
                sync_directory(target, remote=True)
                print(f"FieldBackup Sync: verified {totals}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--status":
        status(sys.argv[2], "Check sync journal" if sys.argv[2] == "SYNC ERROR" else "SMB verified")
    else:
        status("HOME SYNC", "Checking files")
        sync(*sys.argv[1:])
