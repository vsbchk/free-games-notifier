import os
import sqlite3


class State:
    """Durable delivery receipts; database errors must stop the service."""

    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = open(directory / "service.lock", "a+b")
        self.db = None
        try:
            # OS releases the lock after crashes; a stale file does not block startup.
            if os.name == "nt":
                import msvcrt

                self.lock.seek(0)
                self.lock.write(b"0")
                self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(directory / "state.sqlite3")
            self.initialize()
        except Exception:
            self.close()
            raise

    def initialize(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("Unsupported state schema")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS stores (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS offers (key TEXT PRIMARY KEY, suppressed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                offer_key TEXT NOT NULL, channel TEXT NOT NULL,
                PRIMARY KEY (offer_key, channel));
            CREATE TABLE IF NOT EXISTS cooldowns (channel TEXT PRIMARY KEY, until REAL NOT NULL);
            PRAGMA user_version=1;
        """)

    def observe(self, store, offers, notify_existing):
        first = not self.db.execute(
            "SELECT 1 FROM stores WHERE name=?", (store,)
        ).fetchone()
        with self.db:
            for offer in offers:
                self.db.execute(
                    "INSERT OR IGNORE INTO offers VALUES (?, ?)",
                    (offer.key, int(first and not notify_existing)),
                )
            self.db.execute("INSERT OR IGNORE INTO stores VALUES (?)", (store,))

    def pending(self, offer, channel):
        row = self.db.execute(
            "SELECT suppressed FROM offers WHERE key=?", (offer.key,)
        ).fetchone()
        return (
            row is not None
            and not row[0]
            and not self.db.execute(
                "SELECT 1 FROM deliveries WHERE offer_key=? AND channel=?",
                (offer.key, channel),
            ).fetchone()
        )

    def delivered(self, offer, channel):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?)", (offer.key, channel)
            )

    def ready(self, channel, now):
        row = self.db.execute(
            "SELECT until FROM cooldowns WHERE channel=?", (channel,)
        ).fetchone()
        return not row or row[0] <= now

    def defer(self, channel, until):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cooldowns VALUES (?, ?)", (channel, until)
            )

    def close(self):
        if self.db is not None:
            self.db.close()
        self.lock.close()
