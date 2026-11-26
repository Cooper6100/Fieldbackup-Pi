import contextlib
import errno
import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "payload/sd-field-backup"))
sys.path.insert(0, str(ROOT / "payload/fieldbackup-sync"))
import storage
import transfer
from manifest import open_manifest


class SyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.stage = self.root / "stage"
        for path in (self.source, self.destination, self.stage):
            path.mkdir()
        self.rel = Path("photo.jpg")
        self.src = self.source / self.rel
        self.dst = self.destination / self.rel
        self.src.write_bytes(b"original")
        self.manifest = self.root / "manifest.sqlite3"
        patches = patch("transfer.status")
        patches.start()
        self.addCleanup(patches.stop)

    def sync(self, scope="share-A"):
        with contextlib.ExitStack() as stack:
            db = stack.enter_context(open_manifest(self.manifest))
            source, target, stage = [
                stack.enter_context(storage.directory(path))
                for path in (self.source, self.destination, self.stage)
            ]
            return transfer.sync_file(db, scope, self.rel, source, target, stage)

    def test_new_copy_uses_atomic_rename_without_hardlinks(self):
        with patch("os.link", side_effect=OSError(errno.EOPNOTSUPP, "No hard links")):
            self.assertEqual(self.sync(), "COPIED")
        self.assertEqual(self.dst.read_bytes(), b"original")
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_manifest_survives_reopen_and_skips_hashing(self):
        self.assertEqual(self.sync(), "COPIED")
        with patch("storage.digest", side_effect=AssertionError("Cache hit hashed a file")):
            self.assertEqual(self.sync(), "CACHED")
        self.assertEqual(self.sync("share-B"), "SKIPPED")

    def test_source_metadata_change_revalidates(self):
        self.sync()
        os.utime(self.src, ns=(1_000_000_000, 1_000_000_000))
        with patch("storage.digest", wraps=storage.digest) as digest:
            self.assertEqual(self.sync(), "SKIPPED")
            self.assertGreaterEqual(digest.call_count, 2)
        self.assertEqual(self.sync(), "CACHED")

    def test_remote_change_preserves_conflict_and_caches_alternate(self):
        self.sync()
        stamp = self.dst.stat()
        self.dst.write_bytes(b"changed!")
        os.utime(self.dst, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.sync(), "CONFLICT")
        self.assertEqual(self.dst.read_bytes(), b"changed!")
        alternate, = self.destination.glob("conflict-*.jpg")
        self.assertEqual(alternate.read_bytes(), b"original")
        with patch("storage.digest", side_effect=AssertionError("Conflict cache miss")):
            self.assertEqual(self.sync(), "CACHED")
        alternate.write_bytes(b"unrelated")
        with self.assertRaisesRegex(RuntimeError, "conflict copy differs"):
            self.sync()
        self.assertEqual(alternate.read_bytes(), b"unrelated")

    def test_remote_deletion_republishes_verified_data(self):
        self.sync()
        self.dst.unlink()
        self.assertEqual(self.sync(), "COPIED")

    def test_missing_and_invalid_manifest_revalidate(self):
        self.sync()
        self.manifest.unlink()
        self.assertEqual(self.sync(), "SKIPPED")
        self.manifest.write_bytes(b"not a database")
        self.assertEqual(self.sync(), "SKIPPED")
        self.assertEqual(self.sync(), "CACHED")

    def test_invalid_manifest_entry_revalidates(self):
        self.sync()
        with open_manifest(self.manifest) as db, db:
            db.execute("UPDATE verified SET digest = 'bad digest'")
        self.assertEqual(self.sync(), "SKIPPED")
        self.assertEqual(self.sync(), "CACHED")

    def test_competing_writer_between_check_and_rename(self):
        rename = transfer.rename_noreplace

        def compete(stage, temp, dest, name):
            if name == self.rel.name:
                self.dst.write_bytes(b"another writer")
            rename(stage, temp, dest, name)

        with patch("transfer.rename_noreplace", side_effect=compete):
            self.assertEqual(self.sync(), "CONFLICT")
        self.assertEqual(self.dst.read_bytes(), b"another writer")
        self.assertEqual(self.sync(), "CACHED")

    def test_network_failure_before_rename_can_retry(self):
        with patch("transfer.rename_noreplace", side_effect=OSError(errno.EHOSTDOWN, "offline")):
            with self.assertRaises(OSError):
                self.sync()
        self.assertFalse(self.dst.exists())
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertEqual(self.sync(), "COPIED")

    def test_lost_rename_response_revalidates_without_duplicate(self):
        rename = transfer.rename_noreplace

        def disconnect(*args):
            rename(*args)
            raise OSError(errno.ECONNRESET, "network lost after server committed")

        with patch("transfer.rename_noreplace", side_effect=disconnect):
            with self.assertRaises(OSError):
                self.sync()
        self.assertEqual(self.dst.read_bytes(), b"original")
        self.assertEqual(self.sync(), "SKIPPED")
        self.assertEqual(list(self.destination.iterdir()), [self.dst])

    def test_unsupported_rename_fails_without_fallback(self):
        with patch("transfer.rename_noreplace", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
            with self.assertRaises(OSError):
                self.sync()
        self.assertFalse(self.dst.exists())
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_corrupt_staged_file_cannot_be_published(self):
        real_digest = storage.digest

        def corrupt(stream, stop):
            if (self.stage in Path(f"/proc/self/fd/{stream.fileno()}").resolve().parents):
                return "bad data"
            return real_digest(stream, stop)

        with patch("storage.digest", side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                self.sync()
        self.assertFalse(self.dst.exists())

    def test_existing_symlink_fails_closed(self):
        self.dst.symlink_to(self.src)
        with self.assertRaisesRegex(RuntimeError, "Unsafe SMB destination"):
            self.sync()
        self.assertTrue(self.dst.is_symlink())
        self.assertEqual(self.src.read_bytes(), b"original")

    def test_sync_cleans_own_staging_and_can_run_during_import(self):
        machine = self.root / "machine-id"
        machine.write_text("1" * 32)
        share = self.destination
        target = share / "FieldBackup"
        target.mkdir()
        foreign = target / ".fieldbackup-smb-other"
        foreign.mkdir()
        foreign_partial = foreign / ("b" * 32)
        foreign_partial.write_bytes(b"another client")
        (self.source / storage.STAGING).mkdir()
        (self.source / storage.STAGING / ("c" * 32)).write_bytes(b"local import in progress")
        with storage.directory(share) as fd:
            info = {"target": str(share), "id": storage.mount_id(fd), "source": "//server/share",
                    "fstype": "cifs", "fsroot": "/", "options": "rw"}
        with (patch("transfer.backup_volume", side_effect=lambda _: storage.directory(self.source)),
              patch("transfer.configuration", return_value={}), patch("transfer.MANIFEST", self.manifest),
              patch("transfer.MACHINE_ID", machine), patch("transfer.mount_info", return_value=info),
              (self.root / "fieldbackup-transfer.lock").open("w") as import_lock):
            fcntl.flock(import_lock, fcntl.LOCK_EX)
            transfer.sync(str(share), "FieldBackup", "//server/share")
            own, = set(target.glob(".fieldbackup-smb-*")) - {foreign}
            stale = own / ("a" * 32)
            stale.write_bytes(b"abandoned upload")
            (own / "keep").symlink_to(foreign_partial)
            transfer.sync(str(share), "FieldBackup", "//server/share")
            self.assertFalse(stale.exists())
            self.assertTrue((own / "keep").is_symlink())
            self.assertEqual(foreign_partial.read_bytes(), b"another client")
            self.assertEqual((target / self.rel).read_bytes(), b"original")
            self.assertFalse((target / storage.STAGING).exists())

    def test_rejects_smb_stream_and_path_alias_syntax(self):
        for name in ("photo:stream", "sibling\\photo", "photo. ", "photo.", "con.jpg", "LPT1"):
            with self.subTest(name=name):
                self.assertFalse(transfer.smb_safe_component(name))


if __name__ == "__main__":
    unittest.main()
