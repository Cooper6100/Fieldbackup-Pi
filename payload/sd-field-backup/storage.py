"""Mount validation, pinned directory access and verified file publication."""
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

CONFIG = Path("/etc/sd-field-backup.json")
STAGING = ".fieldbackup-staging"
CHUNK = 4 * 1024 * 1024
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def command(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=15).stdout.strip()


def configuration():
    return json.loads(CONFIG.read_text())


def mount_info(path):
    data = json.loads(command("findmnt", "-J", "-T", str(path), "-o",
                              "ID,TARGET,SOURCE,UUID,FSROOT,OPTIONS,MAJ:MIN,FSTYPE"))
    return data["filesystems"][0]


def mount_id(fd):
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise RuntimeError("Unable to identify opened mount")


@contextmanager
def directory(path, *, below=None, create=False, remote=False):
    """Open components without following symlinks or crossing nested mounts below a pin."""
    path = Path(path)
    if ".." in path.parts or path.is_absolute() == (below is not None):
        raise ValueError(f"Unsafe directory path: {path}")
    fd = os.dup(below) if below is not None else os.open("/", DIRECTORY)
    try:
        expected = mount_id(fd) if below is not None else None
        for part in (path.parts if below is not None else path.parts[1:]):
            if create:
                try:
                    os.mkdir(part, 0o750, dir_fd=fd)
                    sync_directory(fd, remote)
                except FileExistsError:
                    pass
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            if expected is not None and mount_id(fd) != expected:
                raise RuntimeError(f"Nested mount refused: {path}")
        yield fd
    finally:
        os.close(fd)


def pinned(fd):
    return Path(f"/proc/self/fd/{fd}")


def physical_device(source):
    device = Path(source).resolve()
    kind = command("lsblk", "-dn", "-o", "TYPE", str(device))
    if kind == "disk":
        return device
    if kind == "part":
        parent = command("lsblk", "-dn", "-o", "PKNAME", str(device))
        if parent:
            return Path("/dev") / parent
    raise RuntimeError(f"Expected a direct disk or partition: {source}")


@contextmanager
def backup_volume(cfg, *, create=False):
    dest = Path(cfg["destination"])
    mount = Path(cfg.get("backup_mount", "/mnt/media"))
    relative = dest.relative_to(mount)
    if not relative.parts or ".." in relative.parts:
        raise RuntimeError("Destination must be a subdirectory of the backup mount")
    expected = cfg.get("backup_uuid", cfg.get("root_uuid"))
    with directory(mount) as volume:
        info = mount_info(mount)
        root = mount_info("/")
        if (not expected or (info["uuid"] or "").lower() != expected.lower()
                or info["target"] != str(mount) or info["fsroot"] != "/"
                or int(info["id"]) != mount_id(volume)
                or "rw" not in info["options"].split(",")
                or info["maj:min"] == root["maj:min"]):
            raise RuntimeError("Expected dedicated writable backup filesystem is not mounted")
        backup_disk = physical_device(info["source"])
        os_disk = physical_device(root["source"])
        if backup_disk == os_disk:
            raise RuntimeError("Backup and OS use the same physical disk")
        if cfg.get("require_nvme", True) and not all(
            disk.name.startswith("nvme") for disk in (backup_disk, os_disk)
        ):
            raise RuntimeError("Separate OS and backup NVMe drives are required")
        with directory(relative, below=volume, create=create) as fd:
            yield fd


def check_stop():
    pass


def digest(stream, stop):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(CHUNK), b""):
        stop()
        result.update(block)
    return result.hexdigest()


class UnsafeFile(RuntimeError):
    pass


def regular_file(path, parent=None):
    descriptor = os.open(path, os.O_PATH | os.O_NOFOLLOW)
    try:
        if (not stat.S_ISREG(os.fstat(descriptor).st_mode)
                or (parent is not None and mount_id(descriptor) != mount_id(parent))):
            raise UnsafeFile(f"Unsafe file: {path}")
        try:
            fd = os.open(pinned(descriptor), os.O_RDONLY | os.O_NOATIME)
        except PermissionError:
            fd = os.open(pinned(descriptor), os.O_RDONLY)
        return os.fdopen(fd, "rb")
    finally:
        os.close(descriptor)


def fingerprint(info):
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


def stable_digest(stream, before, stop):
    result = digest(stream, stop)
    if fingerprint(before) != fingerprint(os.fstat(stream.fileno())):
        raise RuntimeError("File changed during SHA-256 verification")
    return result


def same_file(path, parent, expected, stop):
    try:
        with regular_file(path, parent) as stream:
            return stable_digest(stream, os.fstat(stream.fileno()), stop) == expected
    except FileNotFoundError:
        return None
    except UnsafeFile:
        return False


def sync_directory(fd, remote=False):
    try:
        os.fsync(fd)
    except OSError as error:
        # Some CIFS servers/kernel clients do not support directory fsync.
        if not remote or error.errno not in (errno.EINVAL, errno.EOPNOTSUPP):
            raise


def cleanup_staging(stage, *, remote=False):
    for item in pinned(stage).iterdir():
        name = item.name
        if len(name) == 32 and all(c in "0123456789abcdef" for c in name):
            if stat.S_ISREG(item.lstat().st_mode):
                item.unlink()
    sync_directory(stage, remote)


def copy_verified(src, parent, name, stage, *, reserve=0, stop=check_stop, remote=False, source_parent=None):
    """Hash the source independently, verify the flushed copy, and publish without overwrite."""
    with regular_file(src, source_parent) as reader:
        before = os.fstat(reader.fileno())
        expected = stable_digest(reader, before, stop)
        destination = pinned(parent) / name
        match = same_file(destination, parent, expected, stop)
        if match:
            sync_directory(parent, remote)
            return "SKIPPED"
        # Keep generated conflict names short even when the original name is long.
        name_hash = hashlib.sha256(os.fsencode(name)).hexdigest()[:16]
        conflict = pinned(parent) / f"conflict-{name_hash}-{expected}{Path(name).suffix[:12]}"
        if match is False and same_file(conflict, parent, expected, stop):
            sync_directory(parent, remote)
            return "SKIPPED"
        if shutil.disk_usage(pinned(stage)).free < before.st_size + reserve:
            raise RuntimeError("Insufficient free space after configured reserve")
        temp = pinned(stage) / uuid4().hex
        try:
            reader.seek(0)
            with temp.open("x+b") as writer:
                for block in iter(lambda: reader.read(CHUNK), b""):
                    stop()
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
                with regular_file(temp, stage) as verified:
                    if digest(verified, stop) != expected:
                        raise RuntimeError(f"SHA-256 verification failed: {src}")
                after = os.fstat(reader.fileno())
                if fingerprint(before) != fingerprint(after):
                    raise RuntimeError(f"Source changed during copy: {src}")
                os.utime(writer.fileno(), ns=(before.st_atime_ns, before.st_mtime_ns))
                os.fsync(writer.fileno())
            stop()
            if match is None:
                try:
                    os.link(temp, destination)
                    sync_directory(parent, remote)
                    return "COPIED"
                except FileExistsError:
                    if same_file(destination, parent, expected, stop):
                        sync_directory(parent, remote)
                        return "SKIPPED"
            try:
                os.link(temp, conflict)
                sync_directory(parent, remote)
                return "CONFLICT"
            except FileExistsError:
                if same_file(conflict, parent, expected, stop):
                    sync_directory(parent, remote)
                    return "SKIPPED"
                raise RuntimeError(f"Existing conflict copy differs: {conflict.name}")
        finally:
            temp.unlink(missing_ok=True)
            sync_directory(stage, remote)


def scan_files(root):
    def raise_error(error):
        raise error

    for base, dirs, names in os.walk(root, onerror=raise_error, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        for name in sorted(names):
            path = Path(base) / name
            if not name.startswith(".") and stat.S_ISREG(path.lstat().st_mode):
                yield path
