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


def add_column_if_missing(conn, table, column, definition):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        print(f"添加数据库字段：{table}.{column}")
        return True
    return False


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
        share_scan_rank INTEGER DEFAULT 0,
        discovery_cursor INTEGER NOT NULL DEFAULT 20,
        discovery_fail_rank INTEGER NOT NULL DEFAULT 0,
        discovery_fail_count INTEGER NOT NULL DEFAULT 0,
        last_scan TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS shares (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        seedhub_entry_url TEXT,
        seedhub_rank INTEGER,
        status TEXT NOT NULL DEFAULT 'unknown',
        pool_type TEXT NOT NULL DEFAULT 'front20',
        pending_probe_count INTEGER NOT NULL DEFAULT 0,
        used_count INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        last_check TEXT,
        last_success TEXT,
        last_used_at TEXT,
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
        episode TEXT NOT NULL,
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
        episode TEXT NOT NULL,
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
        episode TEXT NOT NULL,
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

    CREATE TABLE IF NOT EXISTS share_blacklist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id INTEGER NOT NULL,
        pwd_id TEXT NOT NULL,
        url TEXT NOT NULL,
        seedhub_entry_url TEXT,
        reason TEXT NOT NULL DEFAULT 'empty_after_3_scans',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY (show_id)
            REFERENCES shows(id)
            ON DELETE CASCADE,

        UNIQUE(show_id, pwd_id)
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

    CREATE INDEX IF NOT EXISTS idx_share_blacklist_show_pwd
        ON share_blacklist(show_id, pwd_id);
    """)

    add_column_if_missing(conn, "shows", "share_scan_rank", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "shows", "discovery_cursor", "INTEGER NOT NULL DEFAULT 20")
    add_column_if_missing(conn, "shows", "discovery_fail_rank", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(conn, "shows", "discovery_fail_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(conn, "shares", "seedhub_entry_url", "TEXT")
    new_pool_type = add_column_if_missing(conn, "shares", "pool_type", "TEXT NOT NULL DEFAULT 'front20'")
    add_column_if_missing(conn, "shares", "pending_probe_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(conn, "shares", "used_count", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(conn, "shares", "last_used_at", "TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_show_pool_rank ON shares(show_id, pool_type, seedhub_rank)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_show_entry ON shares(show_id, seedhub_entry_url)")

    if new_pool_type:
        conn.execute("""
            UPDATE shares
               SET pool_type = CASE
                   WHEN COALESCE(seedhub_rank,999999) BETWEEN 1 AND 20 THEN 'front20'
                   ELSE 'overflow'
               END
        """)

    conn.execute("UPDATE shows SET discovery_cursor=20 WHERE discovery_cursor IS NULL OR discovery_cursor < 20")
    conn.execute("UPDATE shows SET discovery_fail_rank=0, discovery_fail_count=0 WHERE discovery_fail_rank IS NULL OR discovery_fail_count IS NULL")
    conn.execute("""
        UPDATE shares
           SET pool_type = CASE
               WHEN COALESCE(seedhub_rank,999999) BETWEEN 1 AND 20 THEN 'front20'
               ELSE 'overflow'
           END
         WHERE pool_type IS NULL OR pool_type=''
    """)

    # Legacy DBs may contain shares marked valid even though their scan succeeded with zero
    # usable episode files. Give them the same pending lifecycle as the runtime migration.
    conn.execute("""
        UPDATE shares
           SET status='pending',
               pending_probe_count=CASE WHEN COALESCE(pending_probe_count,0) < 1 THEN 1 ELSE pending_probe_count END,
               fail_count=0,
               last_success=NULL,
               updated_at=CURRENT_TIMESTAMP
         WHERE status='valid'
           AND COALESCE(pending_probe_count,0)=0
           AND last_success IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM share_files sf WHERE sf.share_id=shares.id)
    """)

    conn.commit()
    conn.close()

    print("数据库初始化完成:")
    print(DB_FILE)


if __name__ == "__main__":
    main()
