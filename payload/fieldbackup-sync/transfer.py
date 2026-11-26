"""Synchronize only complete, atomically published backup files."""
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

from manifest import cached_digest, encode, open_manifest, record_verified

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sd-field-backup"))
from storage import (
    backup_volume, check_stop, cleanup_staging, configuration, conflict_name, directory,
    fingerprint, mount_id, mount_info, pinned, regular_file, scan_files, stable_digest,
    staged_copy, sync_directory,
)

RUNTIME = Path("/run/fieldbackup-sync")
MANIFEST = Path("/var/lib/fieldbackup-smb/verified.sqlite3")
MACHINE_ID = Path("/etc/machine-id")
RENAME_NOREPLACE = 1


def status(state, message):
    record = dict(state=state, message=message, updated=time.monotonic())
    temporary = RUNTIME / "status.tmp"
    temporary.write_text(json.dumps(record))
    temporary.replace(RUNTIME / "status.json")


def rename_noreplace(stage, name, parent, destination):
    """CIFS passes no-replace through to the SMB server even across clients."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError:
        raise RuntimeError("SMB publication requires Linux renameat2") from None
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(stage, os.fsencode(name), parent, os.fsencode(destination), RENAME_NOREPLACE):
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(error, "SMB share or kernel rejected atomic no-replace rename", destination)
        raise OSError(error, os.strerror(error), destination)


def metadata(info):
    return (info.st_ino, info.st_mode, *fingerprint(info))


def file_stamp(parent, name):
    """Inspect a directory entry without following links or trusting nested mounts."""
    try:
        fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if mount_id(fd) != mount_id(parent) or not stat.S_ISREG(info.st_mode):
            return "unsafe"
        return metadata(info)
    finally:
        os.close(fd)


def verified_stamp(parent, name, expected):
    before = file_stamp(parent, name)
    if not isinstance(before, tuple):
        return None
    with regular_file(pinned(parent) / name, parent) as reader:
        info = os.fstat(reader.fileno())
        if metadata(info) != before:
            raise RuntimeError("SMB file changed before verification")
        digest = stable_digest(reader, info, check_stop)
    if file_stamp(parent, name) != before:
        raise RuntimeError("SMB file changed during verification")
    return before if digest == expected else None


def sync_file(db, scope, rel, src_parent, dst_parent, stage):
    name = rel.name
    key = os.fsencode(rel)
    source = file_stamp(src_parent, name)
    if not isinstance(source, tuple):
        raise RuntimeError(f"Local source disappeared or is unsafe: {rel}")
    primary = file_stamp(dst_parent, name)
    cached = cached_digest(db, scope, key, source, primary)
    if cached is not None:
        digest, alternate = cached
        if alternate == "null" and isinstance(primary, tuple):
            return "CACHED"
        current = file_stamp(dst_parent, conflict_name(name, digest).rstrip(" ."))
        if isinstance(current, tuple) and encode(current) == alternate:
            return "CACHED"
    if primary is not None and not isinstance(primary, tuple):
        raise RuntimeError(f"Unsafe SMB destination entry: {rel}")

    status("HOME SYNC", name)
    with regular_file(pinned(src_parent) / name, src_parent) as reader:
        before = os.fstat(reader.fileno())
        if metadata(before) != source:
            raise RuntimeError(f"Local file changed before synchronization: {rel}")
        expected = stable_digest(reader, before, check_stop)
        alternate_name = conflict_name(name, expected).rstrip(" .")
        verified = verified_stamp(dst_parent, name, expected)
        if verified is not None:
            record_verified(db, scope, key, expected, source, verified, None)
            return "SKIPPED"
        if primary is not None:
            verified = verified_stamp(dst_parent, alternate_name, expected)
            if verified is not None:
                record_verified(db, scope, key, expected, source, primary, verified)
                return "SKIPPED"

        with staged_copy(reader, before, expected, stage, remote=True) as temp:
            candidates = (name, alternate_name) if primary is None else (alternate_name,)
            for target in candidates:
                try:
                    rename_noreplace(stage, temp.name, dst_parent, target)
                except FileExistsError:
                    verified = verified_stamp(dst_parent, target, expected)
                    if verified is None:
                        if target == alternate_name:
                            raise RuntimeError(f"Existing SMB conflict copy differs: {rel}") from None
                        primary = file_stamp(dst_parent, name)
                        if not isinstance(primary, tuple):
                            raise RuntimeError(f"SMB destination changed during publication: {rel}") from None
                        continue
                    result = "SKIPPED"
                else:
                    result = "COPIED" if target == name else "CONFLICT"
                    sync_directory(dst_parent, remote=True)
                    # Confirm the name still contains the verified data after the rename.
                    verified = verified_stamp(dst_parent, target, expected)
                    if verified is None:
                        raise RuntimeError(f"Published SMB file changed before confirmation: {rel}")
                if target == name:
                    record_verified(db, scope, key, expected, source, verified, None)
                else:
                    record_verified(db, scope, key, expected, source, primary, verified)
                return result


def smb_safe_component(name):
    # Windows/SMB special names and alternate stream syntax may alias other entries.
    return (not re.search(r'[\\/:*?"<>|\x00-\x1f]', name)
            and not name.endswith((" ", "."))
            and not re.fullmatch(r"CON|PRN|AUX|NUL|(?:COM|LPT)[1-9¹²³]",
                                 name.partition(".")[0].rstrip().upper()))


def sync(mount, subfolder, server):
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", subfolder) or not smb_safe_component(subfolder):
        raise ValueError("Unsafe SMB subfolder")
    machine = MACHINE_ID.read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{32}", machine) or machine == "0" * 32:
        raise RuntimeError("A unique initialized /etc/machine-id is required for SMB staging")
    namespace = hashlib.blake2s(b"fieldbackup SMB staging", key=bytes.fromhex(machine), digest_size=16).hexdigest()
    cfg = configuration()
    scope = encode([cfg.get("backup_uuid", cfg.get("root_uuid")), cfg.get("destination"), server, subfolder])
    with backup_volume(cfg) as source, directory(mount) as share:
        info = mount_info(mount)
        if (info["target"] != mount or int(info["id"]) != mount_id(share)
                or info["fstype"] not in ("cifs", "smb3") or info["source"] != server
                or info["fsroot"] != "/" or "rw" not in info["options"].split(",")):
            raise RuntimeError("Expected writable SMB mount")
        with directory(subfolder, below=share, create=True, remote=True) as target:
            with directory(f".fieldbackup-smb-{namespace}", below=target, create=True, remote=True) as stage:
                cleanup_staging(stage, remote=True)
                root = pinned(source)
                totals = dict(COPIED=0, SKIPPED=0, CONFLICT=0, CACHED=0)
                with open_manifest(MANIFEST) as db:
                    for src in scan_files(root):
                        rel = src.relative_to(root)
                        if rel.parts[0] != "Cards" and src.name.endswith(".partial"):
                            continue  # Legacy in-place staging convention
                        if not all(smb_safe_component(part) for part in rel.parts):
                            raise RuntimeError(f"Unsafe SMB path component: {str(rel)!r}")
                        with directory(rel.parent, below=source) as src_parent:
                            with directory(rel.parent, below=target, create=True, remote=True) as dst_parent:
                                result = sync_file(db, scope, rel, src_parent, dst_parent, stage)
                        totals[result] += 1
                sync_directory(target, remote=True)
                print(f"FieldBackup Sync: {totals}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--status":
        status(sys.argv[2], "Check sync journal" if sys.argv[2] == "SYNC ERROR" else "SMB verified")
    else:
        status("HOME SYNC", "Checking files")
        sync(*sys.argv[1:])
