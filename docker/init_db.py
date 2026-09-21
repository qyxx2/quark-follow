import sqlite3
import builtins
from datetime import datetime
from pathlib import Path

DB_FILE = Path("/root/scripts/quark-follow/resource.db")
LOG_FILE = Path(__file__).resolve().parents[1] / "logs" / "init_db.log"


def log_print(*args, **kwargs):
    builtins.print(*args, **kwargs)
    message = kwargs.get("sep", " ").join(str(arg) for arg in args)
    if message:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [INIT] {message}{kwargs.get('end', chr(10))}"
            )


print = log_print


def main():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_FILE)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS shows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        seedhub_url TEXT NOT NULL UNIQUE,
        webdav_path TEXT NOT NULL,
        total_episodes INTEGER,
        latest_episode INTEGER DEFAULT 0,
        last_scan TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS shares (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        seedhub_rank INTEGER,
        status TEXT NOT NULL DEFAULT 'unknown',
        fail_count INTEGER NOT NULL DEFAULT 0,
        last_check TEXT,
        last_success TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY (show_id)
            REFERENCES shows(id)
            ON DELETE CASCADE,

        UNIQUE(show_id, url)
    );

    CREATE TABLE IF NOT EXISTS share_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        share_id INTEGER NOT NULL,
        episode INTEGER NOT NULL,
        filename TEXT NOT NULL,
        size INTEGER,
        last_check TEXT,

        FOREIGN KEY (share_id)
            REFERENCES shares(id)
            ON DELETE CASCADE,

        UNIQUE(share_id, episode, filename)
    );

    CREATE TABLE IF NOT EXISTS webdav_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL,
        episode INTEGER NOT NULL,
        filename TEXT NOT NULL,
        size INTEGER,
        mtime TEXT,
        last_check TEXT,
        source_share_id INTEGER,

        FOREIGN KEY (show_id)
            REFERENCES shows(id)
            ON DELETE CASCADE,

        FOREIGN KEY (source_share_id)
            REFERENCES shares(id)
            ON DELETE SET NULL,

        UNIQUE(show_id, episode, filename)
    );

    CREATE TABLE IF NOT EXISTS replace_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL,
        episode INTEGER NOT NULL,
        source_share_id INTEGER NOT NULL,
        source_file TEXT NOT NULL,
        old_filename TEXT,
        old_size INTEGER,
        new_size INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT,
        error TEXT,

        FOREIGN KEY (show_id)
            REFERENCES shows(id)
            ON DELETE CASCADE,

        FOREIGN KEY (source_share_id)
            REFERENCES shares(id)
            ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_shares_show_rank
        ON shares(show_id, seedhub_rank);

    CREATE INDEX IF NOT EXISTS idx_shares_show_status
        ON shares(show_id, status);

    CREATE INDEX IF NOT EXISTS idx_share_files_share_episode
        ON share_files(share_id, episode);

    CREATE INDEX IF NOT EXISTS idx_webdav_files_show_episode
        ON webdav_files(show_id, episode);

    CREATE INDEX IF NOT EXISTS idx_replace_queue_status
        ON replace_queue(status);

    CREATE INDEX IF NOT EXISTS idx_replace_queue_show_episode
        ON replace_queue(show_id, episode);
    """)

    conn.commit()
    conn.close()

    print("数据库初始化完成:")
    print(DB_FILE)


if __name__ == "__main__":
    main()
