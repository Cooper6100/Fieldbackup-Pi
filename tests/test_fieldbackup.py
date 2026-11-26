import contextlib
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "payload/sd-field-backup"))
sys.path.insert(0, str(ROOT / "payload/fieldbackup-sync"))
import backup
import storage
import transfer


class FileCases(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dest = self.root / "dest"
        self.stage = self.root / storage.STAGING
        self.dest.mkdir()
        self.stage.mkdir()
        self.src = self.root / "original"
        self.src.write_bytes(b"original")
        self.timestamps = (1700000000123456789, 1700000000234567890)
        os.utime(self.src, ns=self.timestamps)

    def copy(self, name="photo.jpg", **options):
        with storage.directory(self.dest) as parent, storage.directory(self.stage) as stage:
            return storage.copy_verified(self.src, parent, name, stage, **options)

    def test_verified_copy_and_timestamps(self):
        self.assertEqual(self.copy(), "COPIED")
        final = self.dest / "photo.jpg"
        st = final.stat()
        self.assertEqual((st.st_atime_ns, st.st_mtime_ns), self.timestamps)
        self.assertEqual(hashlib.sha256(final.read_bytes()).digest(),
                         hashlib.sha256(b"original").digest())
        self.assertEqual(self.copy(reserve=10**18), "SKIPPED")
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_conflict_is_stable_and_original_is_untouched(self):
        name = "long" * 60 + ".jpg"
        existing = self.dest / name
        existing.write_bytes(b"unrelated")
        self.assertEqual(self.copy(name), "CONFLICT")
        self.assertEqual(self.copy(name, reserve=10**18), "SKIPPED")
        self.assertEqual(existing.read_bytes(), b"unrelated")
        alternate, = self.dest.glob("conflict-*.jpg")
        self.assertEqual(alternate.read_bytes(), b"original")

    def test_publish_race_does_not_overwrite(self):
        link = os.link

        def competing_link(source, target):
            if target.name == "photo.jpg":
                target.write_bytes(b"another writer")
            return link(source, target)

        with patch("storage.os.link", side_effect=competing_link):
            self.assertEqual(self.copy(), "CONFLICT")
        self.assertEqual((self.dest / "photo.jpg").read_bytes(), b"another writer")

    def test_file_symlink_conflict_never_follows_link(self):
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"keep")
        (self.dest / "photo.jpg").symlink_to(sentinel)
        self.assertEqual(self.copy(), "CONFLICT")
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_directory_symlink_is_refused(self):
        (self.dest / "redirect").symlink_to(self.root, target_is_directory=True)
        with storage.directory(self.dest) as base:
            with self.assertRaises(OSError), storage.directory("redirect/elsewhere", below=base, create=True):
                pass
        self.assertFalse((self.root / "elsewhere").exists())

    def test_interruption_and_stale_staging_recovery(self):
        stale = self.stage / ("a" * 32)
        stale.write_bytes(b"partial")
        self.assertEqual(self.copy("first.jpg"), "COPIED")

        def interrupt_during_copy():
            interrupt_during_copy.calls += 1
            if interrupt_during_copy.calls == 2:
                raise InterruptedError()

        interrupt_during_copy.calls = 0
        with self.assertRaises(InterruptedError):
            self.copy(stop=interrupt_during_copy)
        self.assertEqual(list(self.stage.iterdir()), [stale])
        self.assertFalse((self.dest / "photo.jpg").exists())
        with storage.directory(self.stage) as stage:
            storage.cleanup_staging(stage)
        self.assertEqual(self.copy("first.jpg"), "SKIPPED")
        self.assertEqual(self.copy(), "COPIED")
        self.assertFalse(stale.exists())

    def test_corrupt_staging_is_not_published(self):
        with patch("storage.digest", side_effect=[hashlib.sha256(b"original").hexdigest(), "incorrect"]):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                self.copy()
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_reserve_blocks_new_publication(self):
        with self.assertRaisesRegex(RuntimeError, "reserve"):
            self.copy(reserve=10**18)
        self.assertEqual(list(self.dest.iterdir()), [])


class MountCases(unittest.TestCase):
    def test_validate_identity_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            cfg = {"destination": str(path / "backup"), "backup_mount": str(path),
                   "backup_uuid": "correct", "require_nvme": True}
            with storage.directory(path) as fd:
                mount_id = storage.mount_id(fd)
            base = {"uuid": "correct", "target": str(path), "fsroot": "/",
                    "id": mount_id, "options": "rw,nosuid", "maj:min": "1:1",
                    "source": "/dev/nvme1n1p1"}
            root = {"maj:min": "2:2", "source": "/dev/nvme0n1p1"}
            for update in ({"uuid": "wrong"}, {"options": "ro"}, {"target": "/"},
                           {"id": -1}, {"fsroot": "/subvolume"}, {"maj:min": "2:2"}):
                with self.subTest(update=update), patch("storage.mount_info", side_effect=[base | update, root]):
                    with self.assertRaises(RuntimeError), storage.backup_volume(cfg, create=True):
                        pass
                self.assertFalse(Path(cfg["destination"]).exists())
            with patch("storage.mount_info", side_effect=[base, root]):
                with patch("storage.physical_device", return_value=Path("/dev/nvme0n1")):
                    with self.assertRaisesRegex(RuntimeError, "same physical"), storage.backup_volume(cfg, create=True):
                        pass
            with patch("storage.mount_info", side_effect=[base, root]):
                with patch("storage.physical_device", side_effect=[Path("/dev/nvme1n1"), Path("/dev/nvme0n1")]):
                    with storage.backup_volume(cfg, create=True) as fd:
                        self.assertEqual(storage.pinned(fd).resolve(), Path(cfg["destination"]))


class ImportAndSyncCases(unittest.TestCase):
    def test_card_full_tree_reimport_and_smb_committed_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "card"
            dest = root / "dest"
            share = root / "share"
            for path in (source, dest, share, source / "DCIM", source / "PRIVATE"):
                path.mkdir()
            for name in ("DCIM/photo.jpg", "PRIVATE/video.mp4", "outside.wav", "original.partial"):
                (source / name).write_bytes(name.encode())
            (source / ".hidden").write_text("exclude")
            (source / "symlink").symlink_to(source / "outside.wav")
            part = {"uuid": "ABCD-1234", "mountpoints": []}
            lights = Mock()
            with storage.directory(share) as fd:
                share_id = storage.mount_id(fd)
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch("backup.backup_volume", side_effect=lambda _: storage.directory(dest)))
                stack.enter_context(patch("backup.source_volume", side_effect=lambda *_: storage.directory(source)))
                stack.enter_context(patch("backup.clock_source", return_value="rtc0"))
                status = stack.enter_context(patch("backup.status"))
                backup.import_card("/dev/sdb1", part, {"reserve_gib": 0}, lights)
                self.assertEqual(status.call_args.kwargs["copied"], 4)
                backup.import_card("/dev/sdb1", part, {"reserve_gib": 0}, lights)
                self.assertEqual(status.call_args.kwargs["skipped"], 4)
            (dest / storage.STAGING / ("a" * 32)).write_bytes(b"in flight")
            (dest / "legacy.partial").write_bytes(b"old staging")
            with patch("transfer.backup_volume", side_effect=lambda _: storage.directory(dest)):
                with patch("transfer.configuration"), patch("transfer.status"):
                    info = {"target": str(share), "id": share_id, "source": "//server/share",
                            "fstype": "cifs", "fsroot": "/", "options": "rw"}
                    with patch("transfer.mount_info", return_value=info):
                        transfer.sync(str(share), "FieldBackup", "//server/share")
            remote = share / "FieldBackup"
            files = {str(p.relative_to(remote)) for p in storage.scan_files(remote)}
            self.assertEqual(files, {f"Cards/abcd-1234/{name}" for name in
                                     ("DCIM/photo.jpg", "PRIVATE/video.mp4", "outside.wav", "original.partial")})

    def test_read_write_source_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            with storage.directory(temp) as fd:
                info = {"target": temp, "id": storage.mount_id(fd), "maj:min": "1:1",
                        "fsroot": "/", "options": "rw", "uuid": "abcd"}
            with patch("backup.mount_info", return_value=info):
                with self.assertRaises(RuntimeError), backup.source_volume(
                    "/dev/sdb1", {"mountpoints": [temp], "maj:min": "1:1", "uuid": "ABCD"}
                ):
                    pass


class ClockCases(unittest.TestCase):
    def test_ntp_or_rtc_and_unreliable_clock(self):
        cfg = {"clock": {"minimum_epoch": 1000, "max_rtc_drift_seconds": 10}}
        with patch("backup.time.time", return_value=2000):
            with patch("backup.command", return_value="yes"):
                self.assertEqual(backup.clock_source(cfg), "NTP")
            with patch("backup.command", side_effect=subprocess.TimeoutExpired("timedatectl", 15)):
                with patch("backup.Path.read_text", return_value="2005"):
                    self.assertEqual(backup.clock_source(cfg), "rtc0")
                with patch("backup.Path.read_text", return_value="2020"):
                    with self.assertRaisesRegex(RuntimeError, "disagree"):
                        backup.clock_source(cfg)
                with patch("backup.Path.read_text", side_effect=FileNotFoundError):
                    with self.assertRaisesRegex(RuntimeError, "unavailable"):
                        backup.clock_source(cfg)
        with patch("backup.time.time", return_value=500):
            with self.assertRaisesRegex(RuntimeError, "too old"):
                backup.clock_source(cfg)


if __name__ == "__main__":
    unittest.main()
