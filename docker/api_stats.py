#!/usr/bin/env python3
"""Low-write API usage statistics for quark-follow.

Per-request callers append one tiny event to a live TSV file.  Normal script
shutdowns flush that file into api_stats.db in one transaction.  The web UI
also reads live files so long-running jobs remain visible before shutdown.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(os.environ.get("QUARK_FOLLOW_BASE") or Path(__file__).resolve().parent.parent)
DB_FILE = ROOT / "api_stats.db"
LOG_DIR = ROOT / "logs"
RETENTION_DAYS = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_call_stats (
    bucket_minute TEXT NOT NULL,
    service TEXT NOT NULL,
    operation TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_minute, service, operation)
);
CREATE INDEX IF NOT EXISTS idx_api_call_stats_bucket
    ON api_call_stats(bucket_minute);
CREATE INDEX IF NOT EXISTS idx_api_call_stats_service_bucket
    ON api_call_stats(service, bucket_minute);
"""


def ensure_db() -> None:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DB_FILE, 0o600)
    except OSError:
        pass
    con = sqlite3.connect(DB_FILE, timeout=3)
    try:
        con.execute("PRAGMA busy_timeout=3000")
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def _safe_field(value: str, fallback: str) -> str:
    value = str(value or "").strip().replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return value or fallback


def _bucket(ts: int | float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:00")


def record_event(event_file: str | Path, service: str, operation: str, failed: bool = False) -> None:
    """Append one event to the per-process live file.

    Recording is deliberately best-effort.  A statistics failure must never
    break the real API request or its surrounding workflow.
    """
    try:
        path = Path(event_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        service = _safe_field(service, "unknown")
        operation = _safe_field(operation, "unknown")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{int(time.time())}\t{service}\t{operation}\t{1 if failed else 0}\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        return


def _read_events(path: Path) -> list[tuple[int, str, str, int]]:
    events: list[tuple[int, str, str, int]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.rstrip("\n\r").split("\t")
                if len(parts) != 4:
                    continue
                try:
                    ts = int(parts[0])
                    failed = 1 if parts[3] == "1" else 0
                except (TypeError, ValueError):
                    continue
                service = _safe_field(parts[1], "unknown")
                operation = _safe_field(parts[2], "unknown")
                events.append((ts, service, operation, failed))
    except OSError:
        pass
    return events


def flush_event_file(event_file: str | Path) -> bool:
    """Merge one live event file into api_stats.db in one transaction.

    Rename the live file before reading it so a late writer cannot be deleted
    together with already-read rows.
    """
    path = Path(event_file)
    if not path.exists():
        return True

    flush_path = path.with_name(f"{path.name}.flushing-{os.getpid()}")
    try:
        path.replace(flush_path)
    except OSError:
        return False

    events = _read_events(flush_path)
    if not events:
        try:
            flush_path.unlink()
        except OSError:
            pass
        return True

    aggregate: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for ts, service, operation, failed in events:
        key = (_bucket(ts), service, operation)
        aggregate[key][0] += 1
        aggregate[key][1] += failed

    try:
        ensure_db()
        con = sqlite3.connect(DB_FILE, timeout=10)
        try:
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("BEGIN IMMEDIATE")
            for (bucket, service, operation), (calls, failures) in aggregate.items():
                con.execute(
                    """
                    INSERT INTO api_call_stats
                        (bucket_minute, service, operation, calls, failures)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bucket_minute, service, operation)
                    DO UPDATE SET
                        calls = calls + excluded.calls,
                        failures = failures + excluded.failures
                    """,
                    (bucket, service, operation, calls, failures),
                )
            cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:00")
            con.execute("DELETE FROM api_call_stats WHERE bucket_minute < ?", (cutoff,))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

        try:
            flush_path.unlink()
        except OSError:
            pass
        return True
    except Exception:
        # Restore the batch to the live file when possible so a failed flush never
        # hides the current counts from the Web UI or loses them permanently.
        try:
            if path.exists():
                with flush_path.open("rb") as src, path.open("ab") as dst:
                    dst.write(src.read())
                flush_path.unlink()
            else:
                flush_path.replace(path)
        except OSError:
            pass
        return False


def _live_events(now: float | None = None) -> list[tuple[int, str, str, int]]:
    now = time.time() if now is None else now
    result: list[tuple[int, str, str, int]] = []
    try:
        paths = LOG_DIR.glob("api_stats_live_*.tsv")
    except OSError:
        return result
    for path in paths:
        for event in _read_events(path):
            # Ignore implausibly old live files; their historical rows remain
            # in the DB after a normal flush, while a stale crash file should
            # not distort current-minute/hour/day figures forever.
            if event[0] >= now - RETENTION_DAYS * 86400:
                result.append(event)
    return result


def _db_rows(since: datetime) -> list[tuple[str, str, str, int, int]]:
    if not DB_FILE.exists():
        return []
    con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=3)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=3000")
        rows = con.execute(
            """
            SELECT bucket_minute, service, operation, calls, failures
            FROM api_call_stats
            WHERE bucket_minute >= ?
            ORDER BY bucket_minute ASC
            """,
            (since.strftime("%Y-%m-%d %H:%M:00"),),
        ).fetchall()
        return [(r[0], r[1], r[2], int(r[3]), int(r[4])) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _combine_rows(now: float | None = None) -> list[tuple[int, str, str, int, int]]:
    now = time.time() if now is None else now
    since = datetime.fromtimestamp(now - RETENTION_DAYS * 86400).replace(second=0, microsecond=0)
    combined: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for bucket, service, operation, calls, failures in _db_rows(since):
        key = (bucket, service, operation)
        combined[key][0] += calls
        combined[key][1] += failures
    for ts, service, operation, failed in _live_events(now):
        bucket = _bucket(ts)
        key = (bucket, service, operation)
        combined[key][0] += 1
        combined[key][1] += failed
    result = []
    for (bucket, service, operation), (calls, failures) in combined.items():
        try:
            ts = int(datetime.strptime(bucket, "%Y-%m-%d %H:%M:00").timestamp())
        except ValueError:
            continue
        result.append((ts, service, operation, calls, failures))
    result.sort(key=lambda x: (x[0], x[1], x[2]))
    return result


def snapshot(now: float | None = None) -> dict:
    """Return UI-friendly summary data without modifying runtime resources."""
    now = time.time() if now is None else now
    rows = _combine_rows(now)
    local_now = datetime.fromtimestamp(now)
    start_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    windows = {
        "1m": now - 60,
        "1h": now - 3600,
        "24h": now - 86400,
        "today": start_today,
    }

    summary = {}
    by_window_operation = {}
    for name, start in windows.items():
        calls = failures = 0
        per_service: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "failures": 0})
        for ts, service, operation, row_calls, row_failures in rows:
            if ts < start or ts > now + 60:
                continue
            calls += row_calls
            failures += row_failures
            per_service[service]["calls"] += row_calls
            per_service[service]["failures"] += row_failures
        summary[name] = {
            "calls": calls,
            "failures": failures,
            "services": dict(per_service),
        }

        op_map: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"calls": 0, "failures": 0})
        for ts, service, operation, row_calls, row_failures in rows:
            if ts < start or ts > now + 60:
                continue
            op_map[(service, operation)]["calls"] += row_calls
            op_map[(service, operation)]["failures"] += row_failures
        by_window_operation[name] = [
            {"service": service, "operation": operation, "calls": data["calls"], "failures": data["failures"]}
            for (service, operation), data in sorted(op_map.items(), key=lambda x: (-x[1]["calls"], x[0][0], x[0][1]))
        ]

    timeline = []
    start_hour = int((now - 23 * 3600) // 3600 * 3600)
    for i in range(24):
        hour_start = start_hour + i * 3600
        item = {
            "time": datetime.fromtimestamp(hour_start).strftime("%m-%d %H:00"),
            "quark": 0,
            "seedhub": 0,
        }
        for ts, service, _operation, calls, _failures in rows:
            if hour_start <= ts < hour_start + 3600:
                if service == "quark":
                    item["quark"] += calls
                elif service == "seedhub":
                    item["seedhub"] += calls
        timeline.append(item)

    return {
        "generated_at": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "operations": by_window_operation,
        "timeline": timeline,
        "retention_days": RETENTION_DAYS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="quark-follow API statistics helper")
    parser.add_argument("--flush", metavar="EVENT_FILE")
    args = parser.parse_args()
    if args.flush:
        # Flush is intentionally best-effort for caller safety.
        flush_event_file(args.flush)
    else:
        ensure_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
