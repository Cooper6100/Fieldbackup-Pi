"""Local, disposable cache of verified source and SMB file fingerprints."""
import json
import re
import sqlite3
from contextlib import contextmanager

VERSION = 1
SCHEMA = """CREATE TABLE verified (
    scope TEXT, path BLOB, digest TEXT, source TEXT, primary_file TEXT, alternate TEXT,
    PRIMARY KEY (scope, path)
) WITHOUT ROWID"""


class InvalidManifest(RuntimeError):
    pass


@contextmanager
def open_manifest(path):
    def connect():
        # StateDirectory is root-only. Keep SQLite's journal on the local OS disk.
        db = sqlite3.connect(path)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and db.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0] == 0:
                with db:
                    db.execute(SCHEMA)
                    db.execute(f"PRAGMA user_version = {VERSION}")
            elif version != VERSION:
                raise InvalidManifest("Unsupported SMB manifest version")
            schema = db.execute("SELECT sql FROM sqlite_schema WHERE name = 'verified'").fetchone()
            if schema != (SCHEMA,) or db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise InvalidManifest("Invalid SMB manifest")
            return db
        except BaseException:
            db.close()
            raise

    try:
        db = connect()
    except (InvalidManifest, sqlite3.DatabaseError) as error:
        code = getattr(error, "sqlite_errorcode", 0) & 0xff
        if not isinstance(error, InvalidManifest) and code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            raise  # A permissions, locking or I/O error is not evidence of corruption.
        print("FieldBackup Sync: invalid manifest; rebuilding from verified files", flush=True)
        path.unlink()
        # A journal from the old database must never be replayed into the fresh cache.
        for suffix in ("-journal", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        db = connect()
    try:
        yield db
    finally:
        db.close()


def encode(stamp):
    return json.dumps(stamp, separators=(",", ":"))


def cached_digest(db, scope, path, source, primary):
    record = db.execute(
        "SELECT digest, source, primary_file, alternate FROM verified WHERE scope = ? AND path = ?",
        (scope, path),
    ).fetchone()
    if record is None:
        return None
    digest, saved_source, saved_primary, alternate = record
    if (isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
            and saved_source == encode(source) and saved_primary == encode(primary)
            and isinstance(alternate, str)):
        return digest, alternate
    return None


def record_verified(db, scope, path, digest, source, primary, alternate):
    with db:
        db.execute("INSERT OR REPLACE INTO verified VALUES (?, ?, ?, ?, ?, ?)",
                   (scope, path, digest, encode(source), encode(primary), encode(alternate)))
