#!/usr/bin/env python3
"""Small, authenticated management UI layered on top of quark-follow scripts."""
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template_string, request, session, url_for

BASE = Path(__file__).resolve().parent
DB = BASE / "resource.db"
RESOURCES = BASE / "resources.json"
CONFIG = BASE / "config.local"
LOG_DIR = BASE / "logs"
API_STATS_PY = BASE / "docker" / "api_stats.py"
LOCKS = {"resource_check": BASE / "resource_check.lock", "replace": BASE / "replace.lock"}
WEB_RESTART_SCRIPT = BASE / "restart-web.sh"
WEB_RESTART_LOCK = BASE / "web-restart.lock"
SEEDHUB_CONTAINER = "seedhub-playwright"
SEEDHUB_CONTAINER_SH = BASE / "seedhub_container.sh"
SEEDHUB_CONTAINER_LOCK = BASE / "seedhub-container.lock"
_CONTAINER_INSPECT_TTL = 2.0
_CONTAINER_SYNC_TTL = 10.0
_container_inspect_cache = None
_container_inspect_cache_at = 0.0
_container_sync_cache = None
_container_sync_cache_at = 0.0
_container_cache_lock = threading.Lock()
USERNAME = os.environ.get("QUARK_WEB_USERNAME")
PASSWORD = os.environ.get("QUARK_WEB_PASSWORD")
if not USERNAME or not PASSWORD:
    raise RuntimeError("Set QUARK_WEB_USERNAME and QUARK_WEB_PASSWORD before starting the web UI.")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("QUARK_WEB_SECRET", secrets.token_hex(32)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("QUARK_WEB_HTTPS") == "1",
)
launch_lock = threading.Lock()
web_restart_requested = False
SENSITIVE = {"QUARK_COOKIE", "OPENLIST_TOKEN", "OPENLIST_PASSWORD", "WEBDAV_PASS"}
LOG_CATEGORIES = {"ALL", "PARSE", "SCAN", "ADD", "REPLACE", "DELETE", "EXCLUDE", "CHECK", "INIT", "WARN", "ERROR"}
CONFIG_FIELDS = [
    ("Quark", [
        ("QUARK_COOKIE", "Cookie", "password"),
        ("QUARK_API_DELAY", "API 间隔（秒）", "number"),
        ("QUARK_TASK_POLL", "任务查询间隔（秒）", "number"),
        ("QUARK_TASK_TIMEOUT", "任务超时（秒）", "number"),
    ]),
    ("OpenList", [
        ("OPENLIST_URL", "OpenList URL", "url"),
        ("OPENLIST_TOKEN", "API Token", "password"),
        ("OPENLIST_PASSWORD", "目标密码", "password"),
    ]),
    ("WebDAV", [
        ("WEBDAV_URL", "WebDAV URL", "url"),
        ("WEBDAV_USER", "用户名", "text"),
        ("WEBDAV_PASS", "密码", "password"),
    ]),
    ("并行任务", [
        ("MAX_PARALLEL_TASKS", "并行剧集数", "number"),
    ]),
    ("媒体过滤", [
        ("VIDEO_MIN_MB", "最小视频 MB", "number"),
        ("VIDEO_MAX_GB", "最大视频 GB（0 不限制）", "number"),
        ("ARCHIVE_LOG_MB", "压缩包日志阈值 MB", "number"),
    ]),
    ("OpenList 刷新", [
        ("OPENLIST_REFRESH_WAIT", "转存后等待（秒）", "number"),
        ("OPENLIST_REFRESH_RETRY", "刷新重试次数", "number"),
        ("OPENLIST_REFRESH_INTERVAL", "刷新间隔（秒）", "number"),
    ]),
    ("资源检查", [
        ("WEBDAV_DEFAULT_ROOT", "默认 WebDAV 根目录", "text"),
        ("RESOURCE_AUTO_ADD", "自动补集", "text"),
        ("RESOURCE_AUTO_DISCOVER", "自动发现", "text"),
        ("RESOURCE_RECHECK_EXISTING_MAX", "已有 Share 重查上限", "number"),
        ("RESOURCE_RECHECK_HOURS", "重查周期（小时）", "number"),
        ("RESOURCE_SEEDHUB_ROUNDS", "SeedHub 增量轮数", "number"),
        ("RESOURCE_TASK_MAX", "单资源最大任务数", "number"),
    ]),
    ("替换", [
        ("REPLACE_ENABLED", "启用替换", "text"),
        ("REPLACE_SOURCE_CHECK_MAX", "替换源检查上限", "number"),
        ("REPLACE_MIN_RATIO", "最小体积比例", "number"),
        ("REPLACE_MIN_GAIN_MB", "最小增益 MB", "number"),
        ("REPLACE_WINDOW_START_HOUR", "开始小时", "number"),
        ("REPLACE_WINDOW_END_HOUR", "结束小时", "number"),
    ]),
    ("安全与日志", [
        ("CURL_TLS_MAX", "CURL TLS 最高版本", "text"),
        ("DRY_RUN", "只扫描（true/false）", "text"),
        ("LOG_DIR", "日志目录（通常留空）", "text"),
        ("RESOURCE_ADD_VERIFY_TIMEOUT", "转存验证超时（秒）", "number"),
        ("RESOURCE_ADD_VERIFY_INTERVAL", "转存验证间隔（秒）", "number"),
    ]),
]
ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\s*)$")
LOG_RE = re.compile(r"^\[(?P<time>[^\]]+)\]\s*\[(?P<category>[A-Z]+)\]\s*(?P<message>.*)$")


_api_stats_snapshot = None
try:
    if API_STATS_PY.is_file():
        _spec = importlib.util.spec_from_file_location("quark_follow_api_stats", API_STATS_PY)
        if _spec and _spec.loader:
            _module = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_module)
            _api_stats_snapshot = _module.snapshot
except Exception:
    _api_stats_snapshot = None


def api_stats_data():
    if _api_stats_snapshot is None:
        return {
            "available": False,
            "error": "API 统计组件不可用。",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                name: {"calls": 0, "failures": 0, "services": {}}
                for name in ("1m", "1h", "24h", "today")
            },
            "operations": {name: [] for name in ("1m", "1h", "24h", "today")},
            "timeline": [],
            "retention_days": 0,
        }
    try:
        data = _api_stats_snapshot()
        data["available"] = True
        return data
    except Exception as exc:
        return {
            "available": False,
            "error": f"API 统计读取失败：{exc}",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                name: {"calls": 0, "failures": 0, "services": {}}
                for name in ("1m", "1h", "24h", "today")
            },
            "operations": {name: [] for name in ("1m", "1h", "24h", "today")},
            "timeline": [],
            "retention_days": 0,
        }


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def db_query(sql, args=(), one=False):
    if not DB.exists():
        return None if one else []
    con = None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=3000")
        rows = con.execute(sql, args).fetchall()
        return dict(rows[0]) if one and rows else (None if one else [dict(r) for r in rows])
    except sqlite3.Error:
        return None if one else []
    finally:
        if con is not None:
            con.close()


PROCESS_DEFINITIONS = [
    ("resource_check", "resource_check.sh", "资源检查"),
    ("source_check", "source_check.sh", "数据库同步"),
    ("seedhub_cache", "seedhub_cache.sh", "SeedHub 解析"),
    ("addfile", "addfile.sh", "补缺转存"),
    ("replace", "replace.sh", "替换"),
    ("web_restart", "restart-web.sh", "Web 重启"),
]


_PROCESS_SNAPSHOT_TTL = 0.5
_process_snapshot_cache = None
_process_snapshot_cache_at = 0.0
_process_snapshot_lock = threading.Lock()


def _invalidate_process_snapshot_cache():
    global _process_snapshot_cache, _process_snapshot_cache_at
    with _process_snapshot_lock:
        _process_snapshot_cache = None
        _process_snapshot_cache_at = 0.0


def _process_snapshot():
    """Return a short-lived snapshot of all UI-visible backend processes."""
    global _process_snapshot_cache, _process_snapshot_cache_at

    now = time.monotonic()
    if _process_snapshot_cache is not None and now - _process_snapshot_cache_at < _PROCESS_SNAPSHOT_TTL:
        return _process_snapshot_cache

    with _process_snapshot_lock:
        now = time.monotonic()
        if _process_snapshot_cache is not None and now - _process_snapshot_cache_at < _PROCESS_SNAPSHOT_TTL:
            return _process_snapshot_cache

        needles = {key: str(BASE / script) for key, script, _label in PROCESS_DEFINITIONS}
        result = {key: [] for key in needles}
        own_pid = os.getpid()

        for proc in Path("/proc").glob("[0-9]*"):
            try:
                pid = int(proc.name)
                if pid == own_pid:
                    continue
                raw = (proc / "cmdline").read_bytes()
                cmd_parts = [part.decode(errors="ignore") for part in raw.split(b"\0") if part]
                if not cmd_parts:
                    continue
                cmd = " ".join(cmd_parts)
                for key, needle in needles.items():
                    if needle in cmd:
                        result[key].append({"pid": pid, "cmd": cmd_parts})
            except (OSError, ValueError):
                pass

        for matches in result.values():
            matches.sort(key=lambda item: item["pid"])

        _process_snapshot_cache = result
        _process_snapshot_cache_at = time.monotonic()
        return result


def process_matches(script, snapshot=None):
    if snapshot is None:
        snapshot = _process_snapshot()
    for key, candidate, _label in PROCESS_DEFINITIONS:
        if candidate == script:
            return snapshot.get(key, [])
    # Preserve the previous generic lookup behavior for callers using a script
    # not listed in PROCESS_DEFINITIONS.
    needle = str(BASE / script)
    matches = []
    own_pid = os.getpid()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc.name)
            if pid == own_pid:
                continue
            raw = (proc / "cmdline").read_bytes()
            cmd_parts = [part.decode(errors="ignore") for part in raw.split(b"\0") if part]
            if needle in " ".join(cmd_parts):
                matches.append({"pid": pid, "cmd": cmd_parts})
        except (OSError, ValueError):
            pass
    return sorted(matches, key=lambda item: item["pid"])


def process_for(script, snapshot=None):
    matches = process_matches(script, snapshot)
    return matches[0]["pid"] if matches else None


def process_target(cmd_parts):
    for flag in ("--show-id", "--share-id"):
        if flag in cmd_parts:
            idx = cmd_parts.index(flag)
            if idx + 1 < len(cmd_parts):
                return f"{flag} {cmd_parts[idx + 1]}"
    return ""


def task_status(snapshot=None):
    if snapshot is None:
        snapshot = _process_snapshot()
    result = {}
    lock_for = {"resource_check": LOCKS["resource_check"], "replace": LOCKS["replace"]}
    for key, script, label in PROCESS_DEFINITIONS:
        matches = process_matches(script, snapshot)
        lock_path = lock_for.get(key)
        lock_exists = bool(lock_path and lock_path.exists())
        result[key] = {
            "label": label,
            "script": script,
            # running includes a known lock for safety. process_running is what
            # the header uses, so a stale lock will not be shown as a running process.
            "running": bool(matches) or lock_exists,
            "process_running": bool(matches),
            "pid": matches[0]["pid"] if matches else None,
            "pids": [item["pid"] for item in matches],
            "target": process_target(matches[0]["cmd"]) if matches else "",
            "lock": lock_exists,
            "lock_only": lock_exists and not matches,
        }
    return result


def background_work_running(state=None):
    if state is None:
        state = task_status()
    for key in ("resource_check", "source_check", "seedhub_cache", "addfile", "replace"):
        if state[key]["running"]:
            return key
    return None


def parse_log_line(line):
    match = LOG_RE.match(line)
    if match:
        event = match.groupdict()
        event["category"] = event["category"].upper()
    else:
        event = {"time": "", "category": "INFO", "message": line}
    text = event["message"].upper()
    if "ERROR" in text:
        event["category"] = "ERROR"
    elif "WARN" in text:
        event["category"] = "WARN"
    return event


def log_events(limit=200, category="all"):
    category = (category or "all").strip().upper()
    if category not in LOG_CATEGORIES:
        category = "ALL"

    lines = []
    for path in sorted(
        LOG_DIR.glob("*.log"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    ):
        try:
            lines.extend(
                (path.name, line.rstrip())
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            )
        except OSError:
            pass

    events = []
    for source, line in lines:
        event = parse_log_line(line)
        event["source"] = source
        if category == "ALL" or event["category"] == category:
            events.append(event)
    return events[-limit:][::-1]


def write_web_log(category, message):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "web_app.log"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{category.upper()}] {message}\n"
            )
        os.chmod(path, 0o600)
    except OSError:
        pass


def atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def remove_resource_from_json(seedhub_url):
    """Remove all matching tracking entries from resources.json.

    Returns:
        (removed_count, original_text)
    """
    if not RESOURCES.exists():
        return 0, None

    original = RESOURCES.read_text(encoding="utf-8")
    data = json.loads(original)

    if isinstance(data, dict):
        entries = data.get("resources")
        if not isinstance(entries, list):
            raise ValueError("resources.json 中的 resources 不是数组。")
        new_entries = [
            entry for entry in entries
            if not isinstance(entry, dict) or entry.get("url") != seedhub_url
        ]
        removed = len(entries) - len(new_entries)
        if removed:
            data["resources"] = new_entries
    elif isinstance(data, list):
        new_entries = [
            entry for entry in data
            if not isinstance(entry, dict) or entry.get("url") != seedhub_url
        ]
        removed = len(data) - len(new_entries)
        data = new_entries
    else:
        raise ValueError("resources.json 顶层格式无效。")

    if removed:
        atomic_write_text(
            RESOURCES,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )
    return removed, original


def delete_show_db(show_id):
    """Delete a show and all known dependent rows in one SQLite transaction."""
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")

        row = con.execute(
            "SELECT id, name, seedhub_url, webdav_path FROM shows WHERE id=?",
            (show_id,),
        ).fetchone()
        if not row:
            con.rollback()
            return None, {}

        counts = {}
        counts["replace_queue"] = con.execute(
            "SELECT COUNT(*) FROM replace_queue WHERE show_id=?",
            (show_id,),
        ).fetchone()[0]
        counts["webdav_files"] = con.execute(
            "SELECT COUNT(*) FROM webdav_files WHERE show_id=?",
            (show_id,),
        ).fetchone()[0]
        counts["shares"] = con.execute(
            "SELECT COUNT(*) FROM shares WHERE show_id=?",
            (show_id,),
        ).fetchone()[0]
        counts["share_files"] = con.execute(
            """
            SELECT COUNT(*)
            FROM share_files
            WHERE share_id IN (SELECT id FROM shares WHERE show_id=?)
            """,
            (show_id,),
        ).fetchone()[0]

        # Explicitly delete children rather than depending solely on a particular
        # database initialization version having ON DELETE CASCADE.
        con.execute("DELETE FROM replace_queue WHERE show_id=?", (show_id,))
        con.execute("DELETE FROM webdav_files WHERE show_id=?", (show_id,))
        con.execute(
            "DELETE FROM share_files WHERE share_id IN (SELECT id FROM shares WHERE show_id=?)",
            (show_id,),
        )
        con.execute("DELETE FROM shares WHERE show_id=?", (show_id,))
        deleted = con.execute("DELETE FROM shows WHERE id=?", (show_id,)).rowcount
        if deleted != 1:
            raise RuntimeError(f"删除 shows.id={show_id} 失败。")

        con.commit()
        return dict(row), counts
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def restore_resource_json(original_text):
    if original_text is not None:
        atomic_write_text(RESOURCES, original_text)


def latest_resource_run():
    path = LOG_DIR / "resource_check.log"
    if not path.exists():
        return {"time": None, "result": "暂无记录"}
    try:
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            event = parse_log_line(line)
            match = re.search(r"resource_check 完成：.*unresolved=(\d+) failed=(\d+)", event["message"])
            if match:
                return {
                    "time": event["time"],
                    "result": "成功" if match.group(1) == match.group(2) == "0" else "失败/未完成",
                }
    except OSError:
        pass
    return {"time": None, "result": "暂无完成记录"}


def _invalidate_container_cache():
    global _container_inspect_cache, _container_inspect_cache_at
    global _container_sync_cache, _container_sync_cache_at
    with _container_cache_lock:
        _container_inspect_cache = None
        _container_inspect_cache_at = 0.0
        _container_sync_cache = None
        _container_sync_cache_at = 0.0


def _docker_bin():
    return shutil.which("docker")


def _docker_run(args, timeout=5):
    docker = _docker_bin()
    if not docker:
        return None, "系统中找不到 docker 命令。"
    try:
        completed = subprocess.run(
            [docker, *args],
            cwd=BASE,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"docker 命令超过 {timeout}s 未返回。"
    except OSError as exc:
        return None, f"无法执行 docker：{exc}"
    return completed, ""


def _container_inspect(force=False):
    global _container_inspect_cache, _container_inspect_cache_at
    now = time.monotonic()
    with _container_cache_lock:
        if (
            not force
            and _container_inspect_cache is not None
            and now - _container_inspect_cache_at < _CONTAINER_INSPECT_TTL
        ):
            return _container_inspect_cache

    docker = _docker_bin()
    if not docker:
        result = {
            "available": False,
            "docker_available": False,
            "exists": False,
            "error": "系统中找不到 docker 命令。",
            "raw": None,
        }
    else:
        completed, error = _docker_run(
            ["inspect", "--format", "{{json .}}", SEEDHUB_CONTAINER],
            timeout=5,
        )
        if completed is None:
            result = {
                "available": False,
                "docker_available": True,
                "exists": False,
                "error": error,
                "raw": None,
            }
        elif completed.returncode != 0:
            stderr = completed.stderr.strip() or "容器不存在。"
            not_found = "No such object" in stderr or "No such container" in stderr or "没有这个容器" in stderr
            result = {
                "available": True,
                "docker_available": True,
                "exists": False,
                "status": "not_found" if not_found else "inspect_error",
                "error": stderr,
                "raw": None,
            }
        else:
            try:
                raw = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                result = {
                    "available": True,
                    "docker_available": True,
                    "exists": True,
                    "error": f"docker inspect 返回的数据无法解析：{exc}",
                    "raw": None,
                }
            else:
                state = raw.get("State") or {}
                result = {
                    "available": True,
                    "docker_available": True,
                    "exists": True,
                    "error": "",
                    "raw": raw,
                    "container": {
                        "id": raw.get("Id") or "",
                        "name": (raw.get("Name") or "").lstrip("/") or SEEDHUB_CONTAINER,
                        "image": raw.get("Config", {}).get("Image") or "",
                        "created": raw.get("Created") or "",
                        "status": state.get("Status") or "unknown",
                        "running": bool(state.get("Running")),
                        "restarting": bool(state.get("Restarting")),
                        "paused": bool(state.get("Paused")),
                        "dead": bool(state.get("Dead")),
                        "started_at": state.get("StartedAt") or "",
                        "finished_at": state.get("FinishedAt") or "",
                        "exit_code": state.get("ExitCode"),
                        "oom_killed": bool(state.get("OOMKilled")),
                        "error": state.get("Error") or "",
                        "restart_count": int(raw.get("RestartCount") or 0),
                    },
                    "mounts": [
                        {
                            "type": item.get("Type") or "",
                            "source": item.get("Source") or "",
                            "destination": item.get("Destination") or "",
                            "rw": bool(item.get("RW")),
                        }
                        for item in (raw.get("Mounts") or [])
                    ],
                }

    with _container_cache_lock:
        _container_inspect_cache = result
        _container_inspect_cache_at = time.monotonic()
    return result


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _host_manifest(root):
    manifest = {}
    if not root.is_dir():
        return manifest
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in files:
            if name.endswith(".pyc"):
                continue
            path = Path(current) / name
            try:
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                manifest[rel] = {
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            except OSError:
                continue
    return manifest


_CONTAINER_MANIFEST_PY = r"""
import hashlib, json, os
root = "/work"
out = {}
for current, dirs, files in os.walk(root):
    dirs[:] = [name for name in dirs if name != "__pycache__"]
    for name in files:
        if name.endswith(".pyc"):
            continue
        path = os.path.join(current, name)
        if not os.path.isfile(path):
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        out[rel] = {"size": os.path.getsize(path), "sha256": digest.hexdigest()}
print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
"""


def _container_manifest():
    completed, error = _docker_run(
        ["exec", SEEDHUB_CONTAINER, "python3", "-c", _CONTAINER_MANIFEST_PY],
        timeout=10,
    )
    if completed is None:
        return None, error
    if completed.returncode != 0:
        return None, completed.stderr.strip() or "无法读取容器 /work 文件。"
    try:
        return json.loads(completed.stdout), ""
    except json.JSONDecodeError as exc:
        return None, f"容器文件清单无法解析：{exc}"


def _container_sync(inspect_data, force=False):
    global _container_sync_cache, _container_sync_cache_at
    now = time.monotonic()
    with _container_cache_lock:
        if (
            not force
            and _container_sync_cache is not None
            and now - _container_sync_cache_at < _CONTAINER_SYNC_TTL
        ):
            return _container_sync_cache

    host_root = BASE / "docker"
    expected_mounts = {
        "/work": host_root.resolve(),
        "/data": BASE.resolve(),
    }
    mounts = {item["destination"]: item for item in inspect_data.get("mounts", [])}
    mount_rows = []
    mounts_ok = True
    for destination, source in expected_mounts.items():
        actual = mounts.get(destination)
        actual_source = Path(actual["source"]).resolve() if actual and actual.get("source") else None
        ok = bool(actual and actual.get("type") == "bind" and actual_source == source)
        if not ok:
            mounts_ok = False
        mount_rows.append({
            "destination": destination,
            "expected_source": str(source),
            "actual_source": str(actual_source) if actual_source else "",
            "type": actual.get("type") if actual else "",
            "rw": actual.get("rw") if actual else None,
            "ok": ok,
        })

    host_manifest = _host_manifest(host_root)
    container_manifest = None
    manifest_error = ""
    verified = False
    comparison = []
    if inspect_data.get("exists") and inspect_data.get("container", {}).get("running") and mounts_ok:
        container_manifest, manifest_error = _container_manifest()
        if container_manifest is not None:
            verified = True
            host_keys = set(host_manifest)
            container_keys = set(container_manifest)
            for rel in sorted(host_keys | container_keys):
                host = host_manifest.get(rel)
                container = container_manifest.get(rel)
                if host is None:
                    status = "container_only"
                elif container is None:
                    status = "host_only"
                elif host == container:
                    status = "same"
                else:
                    status = "different"
                comparison.append({
                    "path": rel,
                    "host": host,
                    "container": container,
                    "status": status,
                })
    elif not mounts_ok:
        manifest_error = "容器挂载关系与当前宿主路径不一致，禁止判定文件同步正常。"
    elif not inspect_data.get("exists"):
        manifest_error = "容器不存在，暂无法读取容器侧文件。"
    else:
        manifest_error = "容器未运行，无法执行容器侧文件清单；当前可校验挂载配置。"

    if verified:
        sync_status = "same" if all(item["status"] == "same" for item in comparison) else "different"
        if not comparison:
            sync_status = "same"
        summary = {
            "total": len(comparison),
            "same": sum(1 for item in comparison if item["status"] == "same"),
            "different": sum(1 for item in comparison if item["status"] == "different"),
            "host_only": sum(1 for item in comparison if item["status"] == "host_only"),
            "container_only": sum(1 for item in comparison if item["status"] == "container_only"),
        }
    elif mounts_ok and inspect_data.get("exists"):
        sync_status = "mount_only"
        summary = {
            "total": len(host_manifest),
            "same": 0,
            "different": 0,
            "host_only": 0,
            "container_only": 0,
        }
    else:
        sync_status = "unavailable"
        summary = {
            "total": len(host_manifest),
            "same": 0,
            "different": 0,
            "host_only": 0,
            "container_only": 0,
        }

    result = {
        "method": "bind_mount",
        "mounts_ok": mounts_ok,
        "mounts": mount_rows,
        "verified": verified,
        "status": sync_status,
        "summary": summary,
        "files": comparison,
        "error": manifest_error,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _container_cache_lock:
        _container_sync_cache = result
        _container_sync_cache_at = time.monotonic()
    return result


def _container_health(inspect_data):
    if not inspect_data.get("exists"):
        return {
            "ready": False,
            "script_exists": False,
            "playwright": False,
            "xvfb": False,
            "mounts_ok": False,
            "error": inspect_data.get("error") or "容器不存在。",
        }
    container = inspect_data.get("container") or {}
    if not container.get("running"):
        return {
            "ready": False,
            "script_exists": False,
            "playwright": False,
            "xvfb": False,
            "mounts_ok": bool(_container_sync(inspect_data).get("mounts_ok")),
            "error": "容器未运行。",
        }

    sync_data = _container_sync(inspect_data)
    mounts_ok = bool(sync_data.get("mounts_ok"))
    checks = {}
    check_specs = {
        "script_exists": ["exec", SEEDHUB_CONTAINER, "test", "-f", "/work/seedhub_cache.py"],
        "playwright": [
            "exec", SEEDHUB_CONTAINER, "python3", "-c",
            "from playwright.sync_api import sync_playwright; import playwright; print('ok')",
        ],
        "xvfb": [
            "exec", SEEDHUB_CONTAINER, "sh", "-c",
            "test -S /tmp/.X11-unix/X99 && command -v Xvfb >/dev/null 2>&1",
        ],
    }
    errors = []
    for key, args in check_specs.items():
        completed, error = _docker_run(args, timeout=5)
        if completed is None:
            checks[key] = False
            errors.append(error)
        else:
            checks[key] = completed.returncode == 0
            if completed.returncode != 0 and completed.stderr.strip():
                errors.append(completed.stderr.strip())

    ready = mounts_ok and all(checks.values())
    return {
        "ready": ready,
        "script_exists": checks["script_exists"],
        "playwright": checks["playwright"],
        "xvfb": checks["xvfb"],
        "mounts_ok": mounts_ok,
        "error": "；".join(dict.fromkeys(errors)) if not ready and errors else ("挂载关系异常。" if not mounts_ok else ""),
    }


def container_data(force=False):
    inspect_data = _container_inspect(force=force)
    if not inspect_data.get("available") or not inspect_data.get("exists"):
        available = bool(inspect_data.get("docker_available"))
        status = inspect_data.get("status") or ("docker_unavailable" if not available else "not_found")
        error = inspect_data.get("error", "Docker 状态读取失败。")
        return {
            "container": {
                "exists": False,
                "docker_available": available,
                "name": SEEDHUB_CONTAINER,
                "status": status,
                "running": False,
                "restarting": False,
                "error": error,
            },
            "health": {
                "ready": False,
                "script_exists": False,
                "playwright": False,
                "xvfb": False,
                "mounts_ok": False,
                "error": error,
            },
            "sync": {
                "method": "bind_mount",
                "mounts_ok": False,
                "verified": False,
                "status": "unavailable",
                "summary": {"total": 0, "same": 0, "different": 0, "host_only": 0, "container_only": 0},
                "files": [],
                "mounts": [],
                "error": error,
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "parser": {"running": False, "pid": None, "target": ""},
            "action_running": SEEDHUB_CONTAINER_LOCK.exists(),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    sync_data = _container_sync(inspect_data)
    health = _container_health(inspect_data)
    container = inspect_data.get("container", {})
    parser_task = task_status().get("seedhub_cache", {})
    recent_parse = log_events(20, "PARSE")
    return {
        "container": {
            **container,
            "exists": True,
            "docker_available": True,
        },
        "health": health,
        "sync": sync_data,
        "parser": {
            "running": bool(parser_task.get("process_running")),
            "pid": parser_task.get("pid"),
            "target": parser_task.get("target") or "",
        },
        "action_running": SEEDHUB_CONTAINER_LOCK.exists(),
        "recent_parse": recent_parse[0] if recent_parse else None,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def container_busy_reason():
    state = task_status()
    for key in ("resource_check", "seedhub_cache"):
        if state.get(key, {}).get("running"):
            return state[key].get("label") or key
    return ""


def start_container_action(action):
    if action not in {"start", "stop", "restart"}:
        abort(404)
    if not SEEDHUB_CONTAINER_SH.is_file():
        write_web_log("ERROR", f"SeedHub 容器操作脚本不存在：{SEEDHUB_CONTAINER_SH}")
        return jsonify({"ok": False, "message": "seedhub_container.sh 不存在，未执行操作。"}), 500

    with launch_lock:
        if SEEDHUB_CONTAINER_LOCK.exists():
            return jsonify({"ok": False, "message": "已有 SeedHub 容器操作正在执行，请等待完成。"}), 409

        if action in {"stop", "restart"}:
            busy = container_busy_reason()
            if busy:
                action_name = "停止" if action == "stop" else "重启"
                write_web_log("WARN", f"SeedHub 容器 {action_name} 被拒绝：后台任务运行中：{busy}")
                return jsonify({"ok": False, "message": f"当前有“{busy}”正在运行，为避免中断 SeedHub 解析，不能{action_name}容器。"}), 409

        log = LOG_DIR / "seedhub_container_launcher.log"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with log.open("ab") as output:
                child = subprocess.Popen(
                    ["/bin/bash", str(SEEDHUB_CONTAINER_SH), action],
                    cwd=BASE,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            write_web_log("ERROR", f"SeedHub 容器操作启动失败：action={action} error={exc}")
            return jsonify({"ok": False, "message": f"无法启动容器操作：{exc}"}), 500

        _invalidate_container_cache()
        write_web_log("INIT", f"SeedHub 容器操作已启动：action={action} pid={child.pid}")
        return jsonify({"ok": True, "action": action, "pid": child.pid})


def dashboard_data():
    task_state = task_status()
    shows = db_query("SELECT COUNT(*) AS n FROM shows", one=True) or {"n": 0}
    shares = db_query(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='dead' THEN 1 ELSE 0 END) AS dead FROM shares",
        one=True,
    ) or {}
    queue = db_query("SELECT status, COUNT(*) AS n FROM replace_queue GROUP BY status")
    counts = {row["status"]: row["n"] for row in queue}
    latest = latest_resource_run()
    return {
        "tasks": task_state,
        "busy": background_work_running(task_state),
        "show_count": shows["n"],
        "share_count": shares.get("total") or 0,
        "dead_shares": shares.get("dead") or 0,
        "queue": {x: counts.get(x, 0) for x in ("pending", "running", "success", "failed")},
        "last_run": latest["time"],
        "last_result": latest["result"],
        "web_pid": os.getpid(),
        "events": log_events(12),
    }


def config_values():
    values = {}
    if not CONFIG.exists():
        return values
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            raw = match.group(4).strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            values[match.group(2)] = raw
    return values


def shell_quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


BASE_TEMPLATE = """<!doctype html><html lang='zh-CN'><meta name='viewport' content='width=device-width,initial-scale=1'><title>quark-follow 管理</title><style>
:root{--bg:#f5f7fb;--card:#fff;--ink:#172033;--blue:#2364d2;--ok:#138a4b;--bad:#c83737;--warn:#aa6900}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px system-ui,-apple-system,"Segoe UI",sans-serif}header{background:#14213d;color:white;padding:11px max(12px,calc((100% - 1000px)/2));display:flex;gap:12px;align-items:center;justify-content:space-between}header .brand{display:flex;align-items:center;gap:8px;min-width:0;flex:1;flex-wrap:wrap}header .brand-name{font-weight:700;white-space:nowrap}nav{display:flex;gap:10px;flex-wrap:wrap}a{color:var(--blue);text-decoration:none}header a{color:#fff}.running-tasks{display:flex;gap:5px;flex-wrap:wrap;align-items:center;min-width:0}.task-pill{display:inline-flex;align-items:center;gap:4px;padding:3px 6px;border:1px solid #ffffff2e;border-radius:999px;background:#ffffff12;color:#eaf0ff;font-size:11px;line-height:1.1;white-space:nowrap}.task-dot{width:5px;height:5px;border-radius:50%;background:#ffd166;display:inline-block;flex:0 0 auto}.container{max-width:1000px;margin:auto;padding:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px}.card,form,.tablewrap{background:var(--card);border-radius:10px;padding:15px;box-shadow:0 1px 3px #0001;margin-bottom:14px}.metric{font-size:25px;font-weight:700}.label{color:#657085;font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}button,.button{border:0;border-radius:7px;background:var(--blue);color:#fff;padding:10px 13px;font:inherit;cursor:pointer}button.secondary{background:#657085}button.danger{background:var(--bad)}button:disabled{opacity:.5;cursor:not-allowed}input,select,textarea{width:100%;padding:9px;border:1px solid #cbd3e1;border-radius:6px;font:inherit}label{display:block;margin:9px 0 4px;font-weight:600}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid #e7eaf0;vertical-align:top}.url{word-break:break-all;overflow-wrap:anywhere}.tablewrap{overflow-x:auto}pre.log{white-space:pre-wrap;overflow-wrap:anywhere;font:12px ui-monospace,SFMono-Regular,monospace;margin:0}.event{padding:8px 0;border-bottom:1px solid #e7eaf0}.tag{font-size:12px;font-weight:bold;padding:2px 5px;border-radius:4px;background:#e8eefc}.flash{padding:10px;background:#e5f7eb;border-radius:7px;margin-bottom:12px}.actions{display:flex;gap:8px;flex-wrap:wrap}.muted{color:#657085}.day-separator{padding:7px;background:#f0f3f8;color:#657085;font-weight:600}.task-actions{align-items:center}.task-actions form{margin:0;padding:0;background:none;box-shadow:none}@media(max-width:560px){header{align-items:flex-start;flex-direction:column;gap:8px}header .brand{width:100%;align-items:flex-start}.running-tasks{width:100%}nav{gap:9px}.container{padding:10px}th,td{padding:7px 5px}}</style><body data-page='{{ page_name|default("") }}'><header><div class='brand'><span class='brand-name'>quark-follow</span><span id='running-tasks' class='running-tasks' aria-live='polite'></span></div><nav><a href='/'>概览</a><a href='/resources'>资源</a><a href='/api-stats'>API</a><a href='/container'>容器</a><a href='/logs'>日志</a><a href='/config'>配置</a><a href='/logout'>退出</a></nav></header><main class='container'>{% with messages=get_flashed_messages() %}{% for m in messages %}<div class='flash'>{{m}}</div>{% endfor %}{% endwith %}<script>(function(){const root=document.getElementById('running-tasks');if(!root)return;if(document.body.dataset.page==='dashboard')return;const escTask=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));async function refreshRunningTasks(){try{const r=await fetch('/api/tasks?ts='+Date.now(),{cache:'no-store'});if(!r.ok)return;const d=await r.json();const active=Object.values(d.tasks||{}).filter(x=>x.process_running);root.innerHTML=active.map(x=>{const target=escTask(x.target||'').replace(/^--show-id /,'资源 #').replace(/^--share-id /,'Share #');return `<span class=task-pill><span class=task-dot></span>${escTask(x.label)}${target?' · '+target:''}</span>`;}).join('');}catch(e){}}refreshRunningTasks();setInterval(refreshRunningTasks,3000);})();</script>"""

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST' and secrets.compare_digest(request.form.get('username',''), USERNAME) and secrets.compare_digest(request.form.get('password',''), PASSWORD):
        session.clear()
        session['authenticated'] = True
        return redirect(request.args.get('next') or url_for('index'))
    return render_template_string(
        """<main class='container'><form method='post'><h1>登录</h1><label>用户名</label><input name='username' required autofocus><label>密码</label><input name='password' type='password' required><p><button>登录</button></p></form></main>"""
        if request.method == 'GET'
        else "<p>登录失败。</p>"
    )

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template_string(
        BASE_TEMPLATE + """<h1>系统概览</h1><div id='dashboard'></div><p class='muted'>页面每 3 秒刷新；运行状态根据实际锁目录及进程检测。</p><script>
let webRestarting=false;
const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
const runningTaskRoot=document.getElementById('running-tasks');
const renderRunningTasks=tasks=>{
  if(!runningTaskRoot)return;
  const active=Object.values(tasks||{}).filter(x=>x.process_running);
  runningTaskRoot.innerHTML=active.map(x=>{
    const target=String(x.target||'').replace(/^--show-id /,'资源 #').replace(/^--share-id /,'Share #');
    return `<span class=task-pill><span class=task-dot></span>${esc(x.label)}${target?' · '+esc(target):''}</span>`;
  }).join('');
};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function waitForWeb(oldPid){
  for(let i=0;i<45;i++){
    await sleep(1000);
    try{
      const r=await fetch('/api/dashboard?restart_probe='+Date.now(),{cache:'no-store'});
      if(r.ok){
        const d=await r.json();
        if(Number(d.web_pid)!==Number(oldPid)){
          location.reload();
          return;
        }
      }
    }catch(e){}
  }
  const btn=document.querySelector('#web-restart');
  if(btn){btn.disabled=false;btn.textContent='重新尝试重启 Web 服务';}
  webRestarting=false;
}
async function restartWeb(oldPid){
  if(webRestarting)return;
  if(!confirm('确定要重启 Web 服务吗？\\n\\n重启过程中网页会短暂断开。\\nresource_check、source_check、addfile、replace 等后台任务不会被停止。'))return;
  webRestarting=true;
  const btn=document.querySelector('#web-restart');
  if(btn){btn.disabled=true;btn.textContent='正在重启…';}
  try{
    const r=await fetch('/tasks/web-restart',{method:'POST'});
    if(!r.ok){
      let msg='Web 重启请求失败。';
      try{const d=await r.json();if(d.message)msg=d.message;}catch(e){}
      if(state)state.textContent=msg;
      if(btn){btn.disabled=false;btn.textContent='重启 Web 服务';}
      webRestarting=false;
      return;
    }
    await waitForWeb(oldPid);
  }catch(e){
    await waitForWeb(oldPid);
  }
}
async function load(){
  if(webRestarting)return;
  try{
    let d=await fetch('/api/dashboard?ts='+Date.now(),{cache:'no-store'}).then(r=>r.json());
    renderRunningTasks(d.tasks);
    let t=d.tasks.resource_check;let q=d.queue;
    document.querySelector('#dashboard').innerHTML=`
      <div class=grid>
        <div class=card><div class=label>Web 服务</div><div class="metric ok">运行中</div><div class=label>PID ${d.web_pid||'未知'} · 端口 5233</div><div class=actions style="margin-top:12px"><button id=web-restart onclick="restartWeb(${Number(d.web_pid)||0})">重启 Web 服务</button></div></div>
        <div class=card><div class=label>resource_check</div><div class="metric ${t.running?'warn':'ok'}">${t.running?'运行中':'空闲'}</div><div class=label>${t.pid?'PID '+t.pid:'无进程'}</div></div>
        <div class=card><div class=label>剧集数量</div><div class=metric>${d.show_count}</div></div>
        <div class=card><div class=label>Share / dead</div><div class=metric>${d.share_count} / ${d.dead_shares}</div></div>
        <div class=card><div class=label>替换队列</div><div>pending ${q.pending} · running ${q.running}<br>success ${q.success} · failed ${q.failed}</div></div>
      </div>
      <div class=card><div class=label>最近一次 resource_check</div>${d.last_run||'暂无'} · ${d.last_result}<div class=actions style="margin-top:12px"><form method=post action='/tasks/resource-check'><button ${d.busy?'disabled':''}>立即运行 resource_check</button></form></div></div>
      <section class=card><h2>最近事件</h2>${d.events.map(e=>`<div class=event><span class=tag>${e.category}</span> <span class=muted>${esc(e.time)} ${esc(e.source)}</span><br>${esc(e.message)}</div>`).join('')||'暂无日志'}</section>`;
  }catch(e){}
}
load();setInterval(load,3000);
</script></main>""",
        page_name='dashboard'
    )

@app.route('/api/dashboard')
@login_required
def api_dashboard():
    return jsonify(dashboard_data())


@app.route('/api/tasks')
@login_required
def api_tasks():
    return jsonify({"tasks": task_status()})

@app.post('/tasks/web-restart')
@login_required
def restart_web():
    global web_restart_requested
    current_pid = os.getpid()
    with launch_lock:
        if web_restart_requested or WEB_RESTART_LOCK.exists():
            return jsonify({"ok": False, "message": "已有 Web 重启任务正在执行，请等待当前重启完成。"}), 409
        if not WEB_RESTART_SCRIPT.is_file():
            write_web_log("ERROR", f"Web 重启请求失败：脚本不存在：{WEB_RESTART_SCRIPT}")
            return jsonify({"ok": False, "message": "restart-web.sh 不存在，未执行重启。"}), 500
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        restart_launcher_log = LOG_DIR / "web_restart_launcher.log"
        write_web_log("INIT", f"收到 Web 重启请求：current_pid={current_pid}")
        try:
            with restart_launcher_log.open("ab") as output:
                child = subprocess.Popen(
                    [str(WEB_RESTART_SCRIPT), str(current_pid)],
                    cwd=BASE,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            _invalidate_process_snapshot_cache()
            web_restart_requested = True
        except OSError as exc:
            write_web_log("ERROR", f"Web 重启启动器启动失败：current_pid={current_pid} error={exc}")
            return jsonify({"ok": False, "message": f"无法启动重启脚本：{exc}"}), 500
    return jsonify({"ok": True, "message": "Web 服务正在重启。", "launcher_pid": child.pid})

@app.route('/resources')
@login_required
def resources():
    rows = db_query(
        """SELECT s.*, COUNT(DISTINCT sh.id) share_count, COUNT(DISTINCT w.episode) owned,
      MAX(w.episode) latest_owned FROM shows s LEFT JOIN shares sh ON sh.show_id=s.id LEFT JOIN webdav_files w ON w.show_id=s.id GROUP BY s.id ORDER BY s.updated_at DESC"""
    )
    for r in rows:
        r['missing'] = max(0, (r['total_episodes'] or 0) - (r['owned'] or 0))
    return render_template_string(
        BASE_TEMPLATE + """<h1>资源</h1><div class=actions><a class=button href='/resources/add'>添加资源</a><a class='button secondary' href='/resources/excluded'>排除列表</a><a class='button secondary' href='/resources/manual'>手动管理资源</a></div><div class=tablewrap><table><tr><th>名称</th><th>总/拥有/缺失</th><th>Share</th><th>状态</th><th>WebDAV 路径</th></tr>{% for r in rows %}<tr><td><a href='/resources/{{r.id}}'>{{r.name}}</a></td><td>{{r.total_episodes or 0}} / {{r.owned or 0}} / {{r.missing}}</td><td>{{r.share_count}}</td><td>{{'已完成' if r.missing == 0 else '待补集'}}</td><td>{{r.webdav_path}}</td></tr>{% else %}<tr><td colspan=5>数据库暂无资源。</td></tr>{% endfor %}</table></div></main>""",
        rows=rows,
    )

@app.route('/resources/<int:show_id>')
@login_required
def resource_detail(show_id):
    show = db_query("SELECT * FROM shows WHERE id=?", (show_id,), True)
    if not show:
        abort(404)
    shares = db_query(
        "SELECT s.*,COUNT(sf.id) file_count,COUNT(DISTINCT sf.episode) episode_count FROM shares s LEFT JOIN share_files sf ON sf.share_id=s.id WHERE s.show_id=? GROUP BY s.id ORDER BY s.seedhub_rank,s.id",
        (show_id,),
    )
    total = show['total_episodes'] or 0

    # webdav_files.episode 使用 SxxEyy 格式；详情页只统计当前资源对应季度。
    season = 1
    show_name = str(show.get('name') or '')
    season_match = re.search(r'第\s*([0-9]+|[零〇兩两一二三四五六七八九十百]+)\s*季', show_name)
    if season_match:
        raw = season_match.group(1).replace('兩', '二').replace('〇', '零')
        if raw.isdigit():
            season = int(raw)
        else:
            cn_digits = {
                '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
                '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
            }
            if raw in cn_digits:
                season = cn_digits[raw]
    else:
        season_match = re.search(r'(?i)\bSeason\s*([0-9]{1,2})\b', show_name)
        if season_match:
            season = int(season_match.group(1))
        else:
            season_match = re.search(r'(?i)(?:^|[^A-Za-z0-9])S\s*([0-9]{1,2})(?:[^A-Za-z0-9]|$)', show_name)
            if season_match:
                season = int(season_match.group(1))

    if season < 1:
        season = 1
    season_prefix = f'S{season:02d}E'
    owned = set()
    for row in db_query(
        "SELECT DISTINCT episode FROM webdav_files WHERE show_id=?",
        (show_id,),
    ):
        episode = str(row.get('episode') or '')
        match = re.fullmatch(rf'{re.escape(season_prefix)}([0-9]+)', episode)
        if match:
            owned.add(int(match.group(1)))

    missing = [x for x in range(1, total + 1) if x not in owned]
    return render_template_string(
        BASE_TEMPLATE + """<h1>{{show.name}}</h1><div class=card><p><b>SeedHub：</b><a href='{{show.seedhub_url}}' rel=noopener>{{show.seedhub_url}}</a></p><p><b>WebDAV：</b>{{show.webdav_path}}</p><p>总集数 {{total}} · 当前集数 {{owned|length}} · 缺失 {{missing|length}}</p><p class=muted>缺失集：{{ missing|join(', ') if missing else '无' }}</p><div class='actions task-actions'><form method=post action='/tasks/source-check/show/{{show.id}}'><button class=secondary>数据库同步</button></form><form method=post action='/tasks/resource-recheck/{{show.id}}'><button>重新检查资源</button></form><form method=post action='/resources/{{show.id}}/delete' onsubmit="return confirm('确定删除此资源？\n\n将删除数据库中的资源及缓存，并从 resources.json 中移除追踪记录。\nWebDAV 中已经存在的文件不会删除。\n此操作不可撤销。');"><button type=submit class=danger>删除资源</button></form></div></div><div class=tablewrap><table><tr><th>rank</th><th>share id</th><th>Share URL</th><th>状态</th><th>失败</th><th>集数统计</th><th>操作</th></tr>{% for s in shares %}<tr><td>{{s.seedhub_rank}}</td><td>{{s.id}}</td><td><a class=url href='{{s.url}}' rel=noopener>{{s.url}}</a></td><td>{{s.status}}</td><td>{{s.fail_count}}</td><td>{{s.episode_count}} 集 / {{s.file_count}} 文件</td><td>{% if s.status == 'excluded' %}<span class=bad>已排除</span>{% else %}<form method=post action='/tasks/source-check/share/{{s.id}}'><button>重扫</button></form><form method=post action='/resources/{{show.id}}/shares/{{s.id}}/exclude'><button type=submit class=danger>排除</button></form>{% endif %}</td></tr>{% endfor %}</table></div></main>""",
        show=show,
        shares=shares,
        total=total,
        owned=owned,
        missing=missing,
    )


def ensure_manual_shares_table():
    con = sqlite3.connect(DB, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("""
            CREATE TABLE IF NOT EXISTS manual_shares (
                share_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (share_id) REFERENCES shares(id) ON DELETE CASCADE
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_manual_shares_share_id ON manual_shares(share_id)")
        con.commit()
    finally:
        con.close()


def manual_share_season(show_name):
    season = 1
    show_name = str(show_name or '')
    season_match = re.search(r'第\s*([0-9]+|[零〇兩两一二三四五六七八九十百]+)\s*季', show_name)
    if season_match:
        raw = season_match.group(1).replace('兩', '二').replace('〇', '零')
        if raw.isdigit():
            season = int(raw)
        else:
            cn_digits = {
                '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
                '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
            }
            if raw in cn_digits:
                season = cn_digits[raw]
    else:
        season_match = re.search(r'(?i)\bSeason\s*([0-9]{1,2})\b', show_name)
        if season_match:
            season = int(season_match.group(1))
        else:
            season_match = re.search(r'(?i)(?:^|[^A-Za-z0-9])S\s*([0-9]{1,2})(?:[^A-Za-z0-9]|$)', show_name)
            if season_match:
                season = int(season_match.group(1))
    return max(1, season)


def manual_share_key(url):
    match = re.fullmatch(r'https?://pan\.quark\.cn/s/([^/#?\s]+)(?:[?#].*)?', str(url or '').strip(), re.I)
    return match.group(1) if match else ''


def add_manual_shares(show_id, raw_urls):
    ensure_manual_shares_table()
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    result = {"added": 0, "existing": 0, "invalid": 0, "excluded": 0}
    seen = set()
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        show = con.execute("SELECT id,name FROM shows WHERE id=?", (show_id,)).fetchone()
        if not show:
            con.rollback()
            return None, result

        for raw in raw_urls:
            raw = raw.strip()
            if not raw:
                continue
            key = manual_share_key(raw)
            if not key or key in seen:
                if not key:
                    result["invalid"] += 1
                continue
            seen.add(key)
            url = f"https://pan.quark.cn/s/{key}"
            row = con.execute(
                "SELECT id,status FROM shares WHERE show_id=? AND url=?",
                (show_id, url),
            ).fetchone()
            if row:
                if row["status"] == "excluded":
                    result["excluded"] += 1
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO manual_shares(share_id) VALUES(?)",
                    (row["id"],),
                )
                if con.execute(
                    "SELECT changes()"
                ).fetchone()[0]:
                    result["added"] += 1
                else:
                    result["existing"] += 1
                continue

            cursor = con.execute(
                "INSERT INTO shares(show_id,url,status,pool_type) VALUES(?,?,?,?)",
                (show_id, url, "pending", "manual"),
            )
            share_id = cursor.lastrowid
            con.execute(
                "INSERT INTO manual_shares(share_id) VALUES(?)",
                (share_id,),
            )
            result["added"] += 1

        con.commit()
        return dict(show), result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def delete_manual_share_db(share_id):
    ensure_manual_shares_table()
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT s.id,s.show_id,s.url,s.status,s.seedhub_rank,s.seedhub_entry_url,sh.name AS show_name
            FROM manual_shares m
            JOIN shares s ON s.id=m.share_id
            JOIN shows sh ON sh.id=s.show_id
            WHERE s.id=?
            """,
            (share_id,),
        ).fetchone()
        if not row:
            con.rollback()
            return None

        auto_managed = row["seedhub_entry_url"] is not None or row["seedhub_rank"] is not None
        if auto_managed:
            con.execute("DELETE FROM manual_shares WHERE share_id=?", (share_id,))
            con.commit()
            return {**dict(row), "auto_managed": True, "webdav_unlinked": 0}

        webdav_unlinked = con.execute(
            "UPDATE webdav_files SET source_share_id=NULL WHERE source_share_id=?",
            (share_id,),
        ).rowcount
        con.execute("DELETE FROM share_files WHERE share_id=?", (share_id,))
        con.execute("DELETE FROM shares WHERE id=?", (share_id,))
        con.commit()
        return {**dict(row), "auto_managed": False, "webdav_unlinked": webdav_unlinked}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@app.route('/resources/manual', methods=['GET', 'POST'])
@login_required
def manual_resources():
    ensure_manual_shares_table()

    if request.method == 'POST':
        show_id = request.form.get('show_id', type=int)
        raw_urls = request.form.get('urls', '').splitlines()
        show, result = add_manual_shares(show_id, raw_urls) if show_id else (None, {"added": 0, "existing": 0, "invalid": 0, "excluded": 0})
        if not show:
            flash('请选择一个有效的剧集。')
        else:
            parts = [f'新增 {result["added"]} 个']
            if result["existing"]:
                parts.append(f'已存在 {result["existing"]} 个')
            if result["invalid"]:
                parts.append(f'无效 {result["invalid"]} 个')
            if result["excluded"]:
                parts.append(f'排除状态 {result["excluded"]} 个（未加入）')
            flash('；'.join(parts) + '。')
        return redirect(url_for('manual_resources'))

    shows = db_query("SELECT id,name,total_episodes FROM shows ORDER BY name COLLATE NOCASE,id")
    rows = db_query(
        """
        SELECT m.share_id,m.created_at,s.show_id,s.url,s.status,s.seedhub_rank,s.seedhub_entry_url,
               sh.name AS show_name,COALESCE(sh.total_episodes,0) AS total_episodes
        FROM manual_shares m
        JOIN shares s ON s.id=m.share_id
        JOIN shows sh ON sh.id=s.show_id
        ORDER BY sh.name COLLATE NOCASE,m.created_at,m.share_id
        """
    )

    share_ids = [r['share_id'] for r in rows]
    show_ids = sorted({r['show_id'] for r in rows})
    share_files = {}
    webdav_files = {}
    if share_ids:
        marks = ','.join('?' for _ in share_ids)
        for r in db_query(
            f"SELECT share_id,episode FROM share_files WHERE share_id IN ({marks})",
            share_ids,
        ):
            share_files.setdefault(r['share_id'], set()).add(str(r['episode']))
    if show_ids:
        marks = ','.join('?' for _ in show_ids)
        for r in db_query(
            f"SELECT show_id,episode FROM webdav_files WHERE show_id IN ({marks})",
            show_ids,
        ):
            webdav_files.setdefault(r['show_id'], set()).add(str(r['episode']))

    status_counts = {}
    useful_shares = 0
    covered_missing = set()
    for r in rows:
        status_counts[r['status']] = status_counts.get(r['status'], 0) + 1
        r['share_key'] = manual_share_key(r['url']) or r['url']
        r['auto_managed'] = r['seedhub_entry_url'] is not None or r['seedhub_rank'] is not None
        r['useful_episodes'] = []
        if r['status'] == 'valid' and r['total_episodes']:
            season = manual_share_season(r['show_name'])
            prefix = f'S{season:02d}E'
            owned = {
                int(ep[len(prefix):])
                for ep in webdav_files.get(r['show_id'], set())
                if ep.startswith(prefix) and ep[len(prefix):].isdigit()
            }
            current = {
                int(ep[len(prefix):])
                for ep in share_files.get(r['share_id'], set())
                if ep.startswith(prefix) and ep[len(prefix):].isdigit()
            }
            r['useful_episodes'] = sorted(
                ep for ep in current
                if 1 <= ep <= r['total_episodes'] and ep not in owned
            )
            if r['useful_episodes']:
                useful_shares += 1
                covered_missing.update((r['show_id'], ep) for ep in r['useful_episodes'])
        r['useful_count'] = len(r['useful_episodes'])
        r['useful_text'] = ', '.join(f'E{ep:02d}' for ep in r['useful_episodes'])

    return render_template_string(
        BASE_TEMPLATE + r"""<h1>手动管理资源</h1>
        <div class=card>
          <p class=muted>这里维护直接的 Quark Share 链接</p>
          <form method=post>
            <label>剧集</label>
            <select name=show_id required>
              <option value="">请选择剧集</option>
              {% for show in shows %}<option value="{{show.id}}">{{show.name}}</option>{% endfor %}
            </select>
            <label>Share 链接（每行一个）</label>
            <textarea name=urls rows=5 placeholder="https://pan.quark.cn/s/xxxxxx&#10;https://pan.quark.cn/s/yyyyyy" required></textarea>
            <p><button>添加链接</button></p>
          </form>
        </div>
        <div class=grid>
          <div class=card><div class=label>手动 Share</div><div class=metric>{{rows|length}}</div></div>
          <div class=card><div class=label>有效</div><div class="metric ok">{{status_counts.get('valid',0)}}</div></div>
          <div class=card><div class=label>待检查</div><div class="metric warn">{{status_counts.get('pending',0) + status_counts.get('unknown',0)}}</div></div>
          <div class=card><div class=label>当前可补 Share</div><div class="metric">{{useful_shares}}</div></div>
          <div class=card><div class=label>可覆盖缺集</div><div class="metric">{{covered_missing|length}}</div></div>
        </div>
        <div class=tablewrap><table>
          <tr><th>Share</th><th>状态</th><th>补集能力</th><th>操作</th></tr>
          {% for r in rows %}
          {% if loop.first or r.show_id != rows[loop.index0 - 1].show_id %}<tr><td colspan=4 class=day-separator><a href='/resources/{{r.show_id}}'>{{r.show_name}}</a></td></tr>{% endif %}
          <tr>
            <td><a href='{{r.url}}' rel=noopener title='{{r.url}}'><code>{{r.share_key}}</code></a></td>
            <td>{% if r.status == 'valid' %}<span class=ok>有效</span>{% elif r.status == 'pending' %}<span class=warn>待检查</span>{% elif r.status == 'dead' %}<span class=bad>失效</span>{% elif r.status == 'excluded' %}<span class=bad>已排除</span>{% else %}{{r.status}}{% endif %}</td>
            <td>{% if r.status == 'valid' and r.useful_count %}<span class=ok>可补 {{r.useful_count}} 集</span><br><span class=muted>{{r.useful_text}}</span>{% elif r.status == 'valid' %}<span class=muted>当前无可补缺集</span>{% elif r.status == 'dead' %}<span class=bad>失效，不能作为补集来源</span>{% else %}<span class=muted>完成检查后判断</span>{% endif %}</td>
            <td><div class='actions task-actions' style='gap:4px;flex-wrap:nowrap'>{% if r.status != 'excluded' %}<form method=post action='/tasks/source-check/share/{{r.share_id}}'><button type=submit style='padding:4px 7px;font-size:16px;line-height:1' title='重扫' aria-label='重扫'><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 0 0-15.1-6.6L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 15.1 6.6L21 16"/><path d="M21 21v-5h-5"/></svg></button></form>{% endif %}<form method=post action='/resources/manual/{{r.share_id}}/delete' onsubmit="return confirm({{('确定从手动管理中删除此 Share？' + ('\\n\\n该 Share 同时属于自动资源，只会从手动列表移除。' if r.auto_managed else '\\n\\n该 Share 仅属于手动资源，将同时删除其缓存来源记录。') )|tojson}})"><button type=submit class=danger style='padding:4px 7px;font-size:16px;line-height:1' title='删除' aria-label='删除'><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/></svg></button></form></div></td>
          </tr>
          {% else %}<tr><td colspan=4>暂无手动 Share。</td></tr>{% endfor %}
        </table></div>
        <p><a class=button href='/resources'>返回资源</a></p>
        </main>""",
        shows=shows,
        rows=rows,
        status_counts=status_counts,
        useful_shares=useful_shares,
        covered_missing=covered_missing,
    )


@app.post('/resources/manual/<int:share_id>/delete')
@login_required
def delete_manual_resource(share_id):
    busy = background_work_running()
    if busy:
        flash(f'当前有 {busy} 任务正在运行，暂不能删除手动 Share。请等待任务结束后再操作。')
        write_web_log('DELETE', f'手动 Share 删除请求被拒绝：share_id={share_id} reason={busy}_running')
        return redirect(url_for('manual_resources'))

    try:
        row = delete_manual_share_db(share_id)
    except sqlite3.Error as exc:
        write_web_log('DELETE', f'删除手动 Share 失败：share_id={share_id} error={exc}')
        flash(f'删除手动 Share 失败：{exc}')
        return redirect(url_for('manual_resources'))

    if not row:
        abort(404)

    if row['auto_managed']:
        write_web_log(
            'DELETE',
            f'手动 Share 已移除但保留自动资源：share_id={share_id} show_id={row["show_id"]} url={row["url"]}',
        )
        flash(f'Share {manual_share_key(row["url"])} 已从手动管理中移除；该 Share 属于自动资源，原记录已保留。')
    else:
        write_web_log(
            'DELETE',
            f'纯手动 Share 已删除：share_id={share_id} show_id={row["show_id"]} url={row["url"]} webdav_unlinked={row["webdav_unlinked"]}',
        )
        flash(f'Share {manual_share_key(row["url"])} 已删除。WebDAV 中已有文件未删除。')
    return redirect(url_for('manual_resources'))


def exclude_share_db(show_id, share_id):
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT id, show_id, url, seedhub_rank, status FROM shares WHERE id=? AND show_id=?",
            (share_id, show_id),
        ).fetchone()
        if not row:
            con.rollback()
            return None, 0, 0
        if row["status"] == "excluded":
            con.commit()
            return dict(row), 0, 0

        cache_count = con.execute(
            "SELECT COUNT(*) FROM share_files WHERE share_id=?", (share_id,)
        ).fetchone()[0]
        queue_count = con.execute(
            "SELECT COUNT(*) FROM replace_queue WHERE source_share_id=? AND status='pending'",
            (share_id,),
        ).fetchone()[0]

        con.execute("DELETE FROM share_files WHERE share_id=?", (share_id,))
        con.execute(
            "DELETE FROM replace_queue WHERE source_share_id=? AND status='pending'",
            (share_id,),
        )
        con.execute(
            "UPDATE shares SET status='excluded', fail_count=0, last_check=NULL, "
            "last_success=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND show_id=?",
            (share_id, show_id),
        )
        con.commit()
        return dict(row), cache_count, queue_count
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def clear_share_exclusion(share_id):
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT id, show_id, url, seedhub_rank, status FROM shares WHERE id=?",
            (share_id,),
        ).fetchone()
        if not row:
            con.rollback()
            return None
        if row["status"] != "excluded":
            con.commit()
            return dict(row)

        con.execute(
            "UPDATE shares SET status='unknown', fail_count=0, last_check=NULL, "
            "last_success=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='excluded'",
            (share_id,),
        )
        con.commit()
        return dict(row)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@app.post('/resources/<int:show_id>/shares/<int:share_id>/exclude')
@login_required
def exclude_share(show_id, share_id):
    busy = background_work_running()
    if busy:
        flash(f'当前有 {busy} 任务正在运行，暂不能排除 Share。请等待任务结束后再操作。')
        write_web_log(
            "EXCLUDE",
            f"排除请求被拒绝：show_id={show_id} share_id={share_id} reason={busy}_running",
        )
        return redirect(request.referrer or url_for("resource_detail", show_id=show_id))

    try:
        row, cache_count, queue_count = exclude_share_db(show_id, share_id)
    except sqlite3.Error as exc:
        write_web_log(
            "EXCLUDE",
            f"排除 Share 失败：show_id={show_id} share_id={share_id} error={exc}",
        )
        flash(f'排除 Share 失败：{exc}')
        return redirect(url_for("resource_detail", show_id=show_id))

    if not row:
        abort(404)
    if row["status"] == "excluded":
        flash(f'Share #{share_id} 已经在排除列表中。')
        return redirect(url_for("resource_detail", show_id=show_id))

    write_web_log(
        "EXCLUDE",
        "Share 已排除："
        f"show_id={show_id} share_id={share_id} rank={row['seedhub_rank']} "
        f"url={row['url']} share_files_deleted={cache_count} "
        f"pending_replace_deleted={queue_count}",
    )
    flash(f'Share #{share_id} 已排除；同时清除了 {cache_count} 条 share_files 缓存。')
    return redirect(url_for("resource_detail", show_id=show_id))


@app.route('/resources/excluded')
@login_required
def excluded_resources():
    rows = db_query(
        "SELECT s.id, s.show_id, s.seedhub_rank, s.url, s.updated_at, "
        "sh.name AS show_name FROM shares s JOIN shows sh ON sh.id=s.show_id "
        "WHERE s.status='excluded' "
        "ORDER BY sh.name, COALESCE(s.seedhub_rank,999999), s.id"
    )
    return render_template_string(
        BASE_TEMPLATE + "<h1>排除列表</h1>"
        "<p class=muted>这里保存被手动排除的 Share。它们不会进入扫描、候选、排名或替换来源。"
        "清除后会恢复为待重新扫描状态。</p>"
        "<div class=tablewrap><table>"
        "<tr><th>share id</th><th>资源</th><th>rank</th><th>Share URL</th><th>操作</th></tr>"
        "{% for r in rows %}"
        "<tr><td>{{r.id}}</td><td><a href='/resources/{{r.show_id}}'>{{r.show_name}}</a></td>"
        "<td>{{r.seedhub_rank}}</td><td><a class=url href='{{r.url}}' rel=noopener>{{r.url}}</a></td>"
        "<td><form method=post action='/resources/excluded/{{r.id}}/clear'><button>清除</button></form></td></tr>"
        "{% else %}<tr><td colspan=5>当前没有排除的 Share。</td></tr>{% endfor %}"
        "</table></div><p><a class=button href='/resources'>返回资源</a></p></main>",
        rows=rows,
    )


@app.post('/resources/excluded/<int:share_id>/clear')
@login_required
def clear_excluded_share(share_id):
    try:
        row = clear_share_exclusion(share_id)
    except sqlite3.Error as exc:
        write_web_log(
            "EXCLUDE",
            f"清除排除失败：share_id={share_id} error={exc}",
        )
        flash(f'清除排除失败：{exc}')
        return redirect(url_for("excluded_resources"))

    if not row:
        abort(404)
    if row["status"] != "excluded":
        flash(f'Share #{share_id} 当前不是排除状态，无需清除。')
        return redirect(url_for("excluded_resources"))

    write_web_log(
        "EXCLUDE",
        f"Share 排除已清除：share_id={share_id} show_id={row['show_id']} "
        f"rank={row['seedhub_rank']} url={row['url']}",
    )
    flash(f'Share #{share_id} 已从排除列表清除；下次资源检查会重新扫描它。')
    return redirect(url_for("excluded_resources"))


@app.post('/resources/<int:show_id>/delete')
@login_required
def delete_resource(show_id):
    show = db_query(
        "SELECT id, name, seedhub_url, webdav_path FROM shows WHERE id=?",
        (show_id,),
        True,
    )
    if not show:
        abort(404)

    busy = background_work_running()
    if busy:
        flash(f'当前有 {busy} 任务正在运行，暂不能删除资源。请等待任务结束后再操作。')
        write_web_log(
            "DELETE",
            f"删除请求被拒绝：show_id={show_id} name={show['name']} reason={busy}_running",
        )
        return redirect(url_for("resource_detail", show_id=show_id))

    original_resources = None
    removed_from_json = 0
    db_deleted = False
    try:
        # 先移除追踪入口，避免删除过程中下一轮 resource_check 又把该资源重新纳入。
        removed_from_json, original_resources = remove_resource_from_json(show["seedhub_url"])

        deleted_show, counts = delete_show_db(show_id)
        if not deleted_show:
            restore_resource_json(original_resources)
            abort(404)
        db_deleted = True

        write_web_log(
            "DELETE",
            "资源删除成功："
            f"show_id={show_id} name={show['name']} "
            f"seedhub_url={show['seedhub_url']} "
            f"webdav_path={show['webdav_path']} "
            f"resources_json_removed={removed_from_json} "
            f"shares={counts['shares']} share_files={counts['share_files']} "
            f"webdav_files={counts['webdav_files']} replace_queue={counts['replace_queue']}",
        )
        flash(
            f"资源“{show['name']}”已删除。数据库缓存和 resources.json 追踪记录已清除；"
            "WebDAV 中已有文件没有删除。"
        )
        return redirect(url_for("resources"))
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error, RuntimeError) as exc:
        # 只有数据库删除尚未提交时才恢复 resources.json。
        # 一旦 DB 已提交删除，绝不能把资源入口重新写回去。
        if not db_deleted:
            try:
                restore_resource_json(original_resources)
            except OSError:
                pass
        write_web_log(
            "DELETE",
            f"删除资源失败：show_id={show_id} name={show['name']} error={exc}",
        )
        flash(f"删除资源失败：{exc}")
        return redirect(url_for("resource_detail", show_id=show_id))


@app.route('/resources/add', methods=['GET','POST'])
@login_required
def add_resource():
    if request.method == 'POST':
        url = request.form.get('url','').strip()
        directory = request.form.get('directory','').strip()
        if not re.match(r'^https?://', url) or (directory and not directory.startswith('/kuake')):
            flash('URL 必须为 HTTP(S)，目录必须以 /kuake 开头。')
        else:
            try:
                data = json.loads(RESOURCES.read_text(encoding='utf-8')) if RESOURCES.exists() else {'resources':[]}
                entries = data if isinstance(data,list) else data.setdefault('resources',[])
                if not any(x.get('url') == url for x in entries):
                    entries.append({'url':url, **({'directory':directory} if directory else {})})
                RESOURCES.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                flash('已写入 resources.json；请运行资源检查以发现该资源。')
                return redirect(url_for('resources'))
            except (OSError,json.JSONDecodeError):
                flash('resources.json 无法读取或写入。')
    return render_template_string(
        BASE_TEMPLATE + """<h1>添加资源</h1><form method=post><label>SeedHub URL</label><input name=url type=url placeholder='https://www.seedhub.cc/movies/xxxx/' required><label>目标父目录</label><input name=directory placeholder='/kuake/电视剧' required><p class=muted>仅修改 resources.json，不会修改 tasks。</p><button>保存资源</button></form></main>"""
    )

@app.post('/tasks/resource-check')
@login_required
def start_resource_check():
    return start_task('resource_check.sh', [], 'resource_check')

@app.post('/tasks/resource-recheck/<int:show_id>')
@login_required
def start_resource_recheck(show_id):
    show = db_query("SELECT id, name, seedhub_url FROM shows WHERE id=?", (show_id,), True)
    if not show:
        abort(404)

    if not RESOURCES.exists():
        flash('resources.json 不存在，无法重新检查此资源。')
        return redirect(url_for('resource_detail', show_id=show_id))

    try:
        data = json.loads(RESOURCES.read_text(encoding='utf-8'))
        entries = data.get('resources') if isinstance(data, dict) else data
        tracked = (
            isinstance(entries, list)
            and any(isinstance(item, dict) and item.get('url') == show['seedhub_url'] for item in entries)
        )
    except (OSError, json.JSONDecodeError):
        tracked = False

    if not tracked:
        flash('该资源不在 resources.json 追踪列表中，未启动重新检查。')
        return redirect(url_for('resource_detail', show_id=show_id))

    return start_task('resource_check.sh', ['--show-id', str(show_id)], '重新检查资源')

@app.post('/tasks/source-check/<kind>/<int:target_id>')
@login_required
def start_source_check(kind, target_id):
    if kind not in ('show','share'):
        abort(404)
    return start_task('source_check.sh', [f'--{kind}-id', str(target_id)], '数据库同步')

def start_task(script, args, label=None):
    with launch_lock:
        state = task_status()
        busy = background_work_running(state)
        if busy:
            display = state.get(busy, {}).get('label', busy)
            flash(f'当前有“{display}”任务正在运行，不能启动新的后台任务。')
            write_web_log('WARN', f"后台任务启动被拒绝：script={script} args={' '.join(args)} reason={busy}_running")
        elif not (BASE / script).is_file():
            flash('脚本不存在，未启动。')
            write_web_log('ERROR', f"后台任务启动失败：脚本不存在：{BASE / script}")
        else:
            log = LOG_DIR / 'web_launcher.log'
            LOG_DIR.mkdir(exist_ok=True)
            try:
                with log.open('ab') as output:
                    child = subprocess.Popen(
                        [str(BASE / script), *args],
                        cwd=BASE,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        close_fds=True,
                    )
                _invalidate_process_snapshot_cache()
                write_web_log('INIT', f"后台任务已启动：script={script} pid={child.pid} args={' '.join(args)} label={label or script}")
                flash(f'已在后台启动“{label or script}”。')
            except OSError as exc:
                write_web_log('ERROR', f"后台任务启动失败：script={script} args={' '.join(args)} error={exc}")
                flash(f'后台任务启动失败：{exc}')
    return redirect(request.referrer or url_for('index'))

@app.route('/api-stats')
@login_required
def api_stats_page():
    return render_template_string(
        BASE_TEMPLATE + """<h1>API 调用统计</h1>
        <div id='stats'></div>
        <p class='muted'>Quark 统计实际 quark_curl 调用；SeedHub 统计 Playwright 页面导航。Web 管理页面自身的 /api/* 请求不计入。统计保留 {{retention_days}} 天；正在运行的任务会先显示实时统计，任务结束后再汇总入库。</p>
        <script>
        const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
        const fmt=n=>Number(n||0).toLocaleString();
        const labels={
          'share/sharepage/token':'获取分享 stoken',
          'share/sharepage/detail':'读取分享目录',
          'file/info/path_list':'读取目标路径 FID',
          'file/sort':'读取目标目录',
          'file/rename':'重命名文件',
          'file/delete':'删除文件',
          'share/sharepage/save':'提交转存',
          'task':'查询转存任务',
          'unknown':'未知接口',
          'movie_page':'SeedHub 一级页面',
          'share_page':'SeedHub 二级页面'
        };
        function opName(service,operation){return labels[operation]||operation;}
        function serviceName(s){return s==='quark'?'Quark API':s==='seedhub'?'SeedHub':'';}
        function card(title,d){
          let q=d?.services?.quark||{calls:0,failures:0};
          let h=d?.services?.seedhub||{calls:0,failures:0};
          return `<div class=card><div class=label>${title}</div><div class=metric>${fmt(d?.calls)}</div><div class=muted>失败 ${fmt(d?.failures)} · Quark ${fmt(q.calls)} / ${fmt(q.failures)} · SeedHub ${fmt(h.calls)} / ${fmt(h.failures)}</div></div>`;
        }
        function render(d){
          let root=document.querySelector('#stats');
          if(!d.available){root.innerHTML=`<div class=card><h2>统计暂不可用</h2><p class=bad>${esc(d.error||'未知错误')}</p></div>`;return;}
          const openDetails=[...root.querySelectorAll('details')].map(x=>x.open);
          let ops=d.operations?.['24h']||[];
          let rows=ops.map(x=>`<tr><td>${esc(serviceName(x.service))}</td><td><code>${esc(x.operation)}</code><br><span class=muted>${esc(opName(x.service,x.operation))}</span></td><td>${fmt(x.calls)}</td><td>${fmt(x.failures)}</td></tr>`).join('');
          const detailTable=`<div class=tablewrap><table><tr><th>服务</th><th>接口</th><th>调用次数</th><th>失败次数</th></tr>${rows||'<tr><td colspan=4>最近 24 小时暂无统计。</td></tr>'}</table></div>`;
          const todayKey=String(d.generated_at||'').slice(5,10);
          const timelineRows=(d.timeline||[])
            .filter(x=>Number(x.quark||0)+Number(x.seedhub||0)>0)
            .map((x,i,a)=>{
              let q=Number(x.quark||0), h=Number(x.seedhub||0), total=Math.max(q,h,1);
              let prev=a[i-1];
              let currentDate=String(x.time||'').slice(0,5);
              let prevDate=prev?String(prev.time||'').slice(0,5):'';
              let label=String(x.time||'').slice(-5);
              let separator='';
              if(i===0 || currentDate!==prevDate){
                let dayLabel=currentDate===todayKey?'今天':'昨天';
                separator=`<tr><th colspan=5 class=day-separator>${dayLabel}</th></tr>`;
              }
              return separator+`<tr><td>${esc(label)}</td><td>${fmt(q)}</td><td>${fmt(h)}</td><td>${fmt(q+h)}</td><td><div style="min-width:160px"><div style="height:8px;background:#e8eefc;border-radius:5px;overflow:hidden"><div style="height:8px;width:${Math.min(100,(q/total)*100)}%;background:#2364d2"></div></div><div style="height:8px;background:#f2e7d3;border-radius:5px;overflow:hidden;margin-top:3px"><div style="height:8px;width:${Math.min(100,(h/total)*100)}%;background:#aa6900"></div></div></div></td></tr>`;
            }).join('');
          const timeline=timelineRows;
          root.innerHTML=`
            <div class=grid>
              ${card('最近 1 分钟',d.summary?.['1m'])}
              ${card('最近 1 小时',d.summary?.['1h'])}
              ${card('最近 24 小时',d.summary?.['24h'])}
              ${card('今日',d.summary?.today)}
            </div>
            <details class=card>
              <summary style="cursor:pointer;font-weight:600">24 小时调用明细</summary>
              <div style="margin-top:12px">${detailTable}</div>
            </details>
            <details class=card>
              <summary style="cursor:pointer;font-weight:600">最近 24 小时逐小时调用量</summary>
              <div style="margin-top:12px"><div class=tablewrap><table><tr><th>时间</th><th>Quark</th><th>SeedHub</th><th>合计</th><th>相对量</th></tr>${timeline||'<tr><td colspan=5>最近 24 小时暂无调用。</td></tr>'}</table></div></div>
            </details>
            <div class=muted style="margin:6px 0 18px">最后更新：${esc(d.generated_at||'')} · 统计数据库保留 ${fmt(d.retention_days)} 天</div>`;
          [...root.querySelectorAll('details')].forEach((el,i)=>{ if(openDetails[i]) el.open=true; });
        }
        async function loadStats(){
          try{
            const r=await fetch('/api/stats?ts='+Date.now(),{cache:'no-store'});
            if(!r.ok) throw new Error('HTTP '+r.status);
            render(await r.json());
          }catch(e){
            document.querySelector('#stats').innerHTML=`<div class=card><h2>统计读取失败</h2><p class=bad>${esc(e.message||e)}</p></div>`;
          }
        }
        loadStats();setInterval(loadStats,5000);
        </script></main>""",
        retention_days=api_stats_data().get('retention_days',0),
    )


@app.route('/api/stats')
@login_required
def api_stats():
    return jsonify(api_stats_data())


@app.route('/container')
@login_required
def container_page():
    return render_template_string(
        BASE_TEMPLATE + r"""<h1>SeedHub 解析容器</h1>
        <div id='container-state'></div>
        <p class='muted'>页面自动刷新；容器操作只允许固定的启动、停止、重启，不提供任意 Docker 命令。停止/重启会在 resource_check 或 SeedHub 解析运行时自动拒绝。</p>
        <script>
        const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
        const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
        let actionBusy=false;
        function statusText(c){
          if(!c?.docker_available)return 'Docker 不可用';
          if(!c?.exists)return '不存在';
          if(c.restarting||c.status==='restarting')return '重启中';
          if(c.running)return '运行中';
          return '已停止';
        }
        function statusClass(c){return !c?.docker_available||!c?.exists?'bad':(c.restarting||c.status==='restarting')?'warn':c.running?'ok':'bad';}
        function syncText(s){return s.status==='same'?'文件一致':s.status==='different'?'存在差异':s.status==='mount_only'?'Bind Mount 已确认':'暂不可验证';}
        function syncClass(s){return s.status==='same'||s.status==='mount_only'?'ok':s.status==='different'?'bad':'warn';}
        function fmtSize(n){n=Number(n||0);if(n<1024)return n+' B';if(n<1024*1024)return (n/1024).toFixed(1)+' KB';if(n<1024*1024*1024)return (n/1024/1024).toFixed(1)+' MB';return (n/1024/1024/1024).toFixed(2)+' GB';}
        async function doAction(action){
          if(actionBusy)return;
          let d=window.currentContainerData||{};
          if((action==='stop'||action==='restart') && d.parser?.running){alert('当前正在执行 SeedHub 解析，不能停止或重启容器。请等待解析结束。');return;}
          if(action==='stop' && d.container?.exists && !d.container.running)return;
          const messages={start:'确定启动 SeedHub 解析容器吗？',stop:'确定停止 SeedHub 解析容器吗？\n\n如果 resource_check 或 SeedHub 解析正在运行，后端会拒绝此操作。',restart:'确定重启 SeedHub 解析容器吗？\n\n如果 resource_check 或 SeedHub 解析正在运行，后端会拒绝此操作。'};
          if(!confirm(messages[action]))return;
          actionBusy=true;
          try{
            const r=await fetch('/tasks/container/'+action,{method:'POST',headers:{'Content-Type':'application/json'}});
            const body=await r.json().catch(()=>({}));
            if(!r.ok){alert(body.message||('容器操作请求失败。'));actionBusy=false;return;}
          }catch(e){alert('容器操作请求失败：'+(e.message||e));actionBusy=false;return;}
          for(let i=0;i<25;i++){
            await sleep(800);
            try{
              const r=await fetch('/api/container?ts='+Date.now(),{cache:'no-store'});
              if(r.ok){window.currentContainerData=await r.json();render(window.currentContainerData);if(!window.currentContainerData.action_running)break;}
            }catch(e){}
          }
          actionBusy=false;
          load();
        }
        function render(d){
          window.currentContainerData=d;
          const c=d.container||{};const h=d.health||{};const s=d.sync||{};const parser=d.parser||{};const root=document.querySelector('#container-state');
          const actionRunning=d.action_running;
          const canStop=c.exists&&c.running&&!actionRunning&&!parser.running;
          const canStart=(!c.exists||!c.running)&&!actionRunning;
          const canRestart=c.exists&&!actionRunning&&!parser.running;
          const files=(s.files||[]).map(x=>{
            const hs=x.host?.sha256?x.host.sha256.slice(0,12):'—';const cs=x.container?.sha256?x.container.sha256.slice(0,12):'—';
            const state=x.status==='same'?'一致':x.status==='different'?'差异':x.status==='host_only'?'宿主独有':'容器独有';
            return `<tr><td><code>${esc(x.path)}</code></td><td>${esc(fmtSize(x.host?.size))}<br><span class=muted>${hs}</span></td><td>${esc(fmtSize(x.container?.size))}<br><span class=muted>${cs}</span></td><td class=${x.status==='same'?'ok':'bad'}>${state}</td></tr>`;
          }).join('');
          const mounts=(s.mounts||[]).map(x=>`<tr><td><code>${esc(x.destination)}</code></td><td><code>${esc(x.actual_source||x.expected_source)}</code></td><td>${x.type==='bind'?'Bind Mount':esc(x.type||'异常')}</td><td>${x.rw===true?'读写':x.rw===false?'只读':'—'}</td><td class=${x.ok?'ok':'bad'}>${x.ok?'正常':'异常'}</td></tr>`).join('');
          root.innerHTML=`
          <div class=grid>
            <div class=card><div class=label>容器状态</div><div class="metric ${statusClass(c)}">${esc(statusText(c))}</div><div class=label>${esc(c.name||'seedhub-playwright')} · ${esc(c.image||'未知镜像')}</div><div class=actions style="margin-top:12px"><button onclick="doAction('start')" ${canStart?'':'disabled'}>启动</button><button class=secondary onclick="doAction('restart')" ${canRestart?'':'disabled'}>重启</button><button class=danger onclick="doAction('stop')" ${canStop?'':'disabled'}>停止</button></div>${actionRunning?'<p class=warn>容器操作正在执行…</p>':''}</div>
            <div class=card><div class=label>解析环境</div><div class="metric ${h.ready?'ok':'warn'}">${h.ready?'就绪':'异常/未就绪'}</div><div>seedhub_cache.py：${h.script_exists?'<span class=ok>✓</span>':'<span class=bad>✗</span>'} · Playwright：${h.playwright?'<span class=ok>✓</span>':'<span class=bad>✗</span>'} · Xvfb：${h.xvfb?'<span class=ok>✓</span>':'<span class=bad>✗</span>'}</div><div class=muted>${esc(h.error||'')}</div></div>
            <div class=card><div class=label>当前 SeedHub 任务</div><div class="metric ${parser.running?'warn':'ok'}">${parser.running?'解析中':'空闲'}</div><div class=label>${parser.pid?'PID '+esc(parser.pid):'无进程'}${parser.target?' · '+esc(parser.target):''}</div><div class=actions style="margin-top:12px"><a class='button secondary' href='/logs?category=PARSE'>查看解析日志</a></div></div>
          </div>
          <div class=card><div class=label>宿主机 / 容器文件状态</div><div class="metric ${syncClass(s)}">${esc(syncText(s))}</div><p></p><p>${esc(s.error||'')}</p><div class=muted>已验证 ${s.summary?.same||0} / ${s.summary?.total||0} 个文件${s.verified?' · SHA256 已实际比对':' · 当前为挂载关系校验，容器侧清单暂不可用'}</div><div class=tablewrap style="margin-top:12px"><table><tr><th>文件</th><th>宿主机</th><th>容器</th><th>状态</th></tr>${files||'<tr><td colspan=4>当前无法取得容器侧文件清单；请先启动容器。</td></tr>'}</table></div></div>
          <div class=card><div class=label>挂载检查</div><div class="metric ${s.mounts_ok?'ok':'bad'}">${s.mounts_ok?'正常':'异常'}</div><div class=tablewrap><table><tr><th>容器路径</th><th>宿主来源</th><th>类型</th><th>权限</th><th>状态</th></tr>${mounts||'<tr><td colspan=5>暂无挂载信息。</td></tr>'}</table></div></div>
          <div class=card><div class=label>容器诊断</div><div class=tablewrap><table><tr><th>项目</th><th>值</th><th>项目</th><th>值</th></tr><tr><td>Container ID</td><td><code>${esc((c.id||'').slice(0,16)||'—')}</code></td><td>Restart Count</td><td>${c.restart_count??'—'}</td></tr><tr><td>Created</td><td>${esc(c.created||'—')}</td><td>Started</td><td>${esc(c.started_at||'—')}</td></tr><tr><td>Exit Code</td><td>${c.exit_code??'—'}</td><td>OOMKilled</td><td class=${c.oom_killed?'bad':'ok'}>${c.oom_killed?'是':'否'}</td></tr><tr><td>Last Error</td><td colspan=3 class=${c.error?'bad':''}>${esc(c.error||'无')}</td></tr></table></div></div>
          <div class=card><div class=label>最近一次 SeedHub 日志</div>${d.recent_parse?`<span class=tag>PARSE</span> <span class=muted>${esc(d.recent_parse.time)} · ${esc(d.recent_parse.source)}</span><pre class=log>${esc(d.recent_parse.message)}</pre>`:'暂无解析日志'}</div>`;
        }
        async function load(){if(actionBusy)return;try{const r=await fetch('/api/container?ts='+Date.now(),{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);render(await r.json());}catch(e){document.querySelector('#container-state').innerHTML=`<div class=card><h2>容器状态读取失败</h2><p class=bad>${esc(e.message||e)}</p></div>`;}}
        load();setInterval(load,3000);
        </script></main>""",
    )


@app.route('/api/container')
@login_required
def api_container():
    return jsonify(container_data())


@app.post('/tasks/container/start')
@login_required
def start_container():
    return start_container_action('start')


@app.post('/tasks/container/stop')
@login_required
def stop_container():
    return start_container_action('stop')


@app.post('/tasks/container/restart')
@login_required
def restart_container():
    return start_container_action('restart')


@app.route('/logs')
@login_required
def logs():
    category = request.args.get('category','all').strip().upper()
    if category not in LOG_CATEGORIES:
        category = 'ALL'
    category = category.lower()
    return render_template_string(
        BASE_TEMPLATE + """<h1>日志</h1><div class=actions>{% for c in cats %}<a class='button' href='/logs?category={{c}}'>{{c}}</a>{% endfor %}</div><div id=events class=card style='margin-top:14px'></div><script>const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));async function load(){let e=await fetch('/api/logs?category={{category}}').then(r=>r.json());document.querySelector('#events').innerHTML=e.map(x=>`<div class=event><span class=tag>${x.category}</span> <span class=muted>${esc(x.time)} · ${esc(x.source)}</span><pre class=log>${esc(x.message)}</pre></div>`).join('')||'暂无匹配日志'}load();setInterval(load,3000)</script></main>""",
        cats=['all','PARSE','SCAN','ADD','REPLACE','DELETE','EXCLUDE','CHECK','INIT','WARN','ERROR'],
        category=category,
    )

@app.route('/api/logs')
@login_required
def api_logs():
    category = request.args.get('category','all').strip().upper()
    if category not in LOG_CATEGORIES:
        abort(400)
    return jsonify(
        log_events(
            min(max(request.args.get('limit',200,type=int),1),500),
            category,
        )
    )

@app.route('/config', methods=['GET','POST'])
@login_required
def config():
    if request.method == 'POST':
        if not CONFIG.exists():
            flash('config.local 不存在，未保存。')
        else:
            supplied = {
                key: request.form[key]
                for _, fields in CONFIG_FIELDS
                for key,_,_ in fields
                if key in request.form
            }
            original = CONFIG.read_text(encoding='utf-8')
            CONFIG.with_suffix('.local.bak').write_text(original,encoding='utf-8')
            out = []
            for line in original.splitlines(keepends=True):
                match = ASSIGNMENT.match(line.rstrip('\n'))
                if match and match.group(2) in supplied:
                    key = match.group(2)
                    value = supplied[key]
                    if key in SENSITIVE and not value:
                        out.append(line)
                    else:
                        out.append(f'{match.group(1)}{key}{match.group(3)}{shell_quote(value)}\n')
                else:
                    out.append(line)
            CONFIG.write_text(''.join(out),encoding='utf-8')
            os.chmod(CONFIG,0o600)
            flash('已保存 config.local，并创建 config.local.bak。')
            return redirect(url_for('config'))

    values = config_values()
    groups = []
    for title,fields in CONFIG_FIELDS:
        groups.append(
            (
                title,
                [
                    {
                        'key':k,
                        'label':l,
                        'type':t,
                        'value': '' if k in SENSITIVE else values.get(k, ''),
                        'sensitive':k in SENSITIVE
                    }
                    for k,l,t in fields
                ],
            )
        )
    return render_template_string(
        BASE_TEMPLATE + """<h1>配置</h1><form method=post>{% for title,fields in groups %}<section class=card><h2>{{title}}</h2>{% for f in fields %}<label>{{f.label}}</label><input name='{{f.key}}' type='{{f.type}}' value='{{f.value}}' {% if f.sensitive %}placeholder='留空则保持原值' autocomplete='new-password'{% endif %}>{% endfor %}</section>{% endfor %}<button>保存配置</button><p class=muted>敏感字段不会返回给浏览器；留空即可保留原值。只会更新本表单列出的现有配置项。</p></form></main>""",
        groups=groups,
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5233)
