#!/usr/bin/env python3
"""Small, authenticated management UI layered on top of quark-follow scripts."""
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template_string, request, session, url_for

BASE = Path(__file__).resolve().parent
DB = BASE / "resource.db"
RESOURCES = BASE / "resources.json"
CONFIG = BASE / "config.local"
LOG_DIR = BASE / "logs"
LOCKS = {"resource_check": BASE / "resource_check.lock", "replace": BASE / "replace.lock"}
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


def process_for(script):
    needle = str(BASE / script)
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
            if needle in cmd and proc.name != str(os.getpid()):
                return int(proc.name)
        except (OSError, ValueError):
            pass
    return None


def task_status():
    result = {}
    for name, lock in LOCKS.items():
        script = "resource_check.sh" if name == "resource_check" else "replace.sh"
        pid = process_for(script)
        result[name] = {"running": lock.exists() or pid is not None, "pid": pid, "lock": lock.exists()}
    return result


def background_work_running():
    state = task_status()
    if state["resource_check"]["running"]:
        return "resource_check"
    if process_for("source_check.sh") is not None:
        return "source_check"
    if process_for("addfile.sh") is not None:
        return "addfile"
    if state["replace"]["running"]:
        return "replace"
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


def dashboard_data():
    shows = db_query("SELECT COUNT(*) AS n FROM shows", one=True) or {"n": 0}
    shares = db_query(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='dead' THEN 1 ELSE 0 END) AS dead FROM shares",
        one=True,
    ) or {}
    queue = db_query("SELECT status, COUNT(*) AS n FROM replace_queue GROUP BY status")
    counts = {row["status"]: row["n"] for row in queue}
    latest = latest_resource_run()
    return {
        "tasks": task_status(),
        "show_count": shows["n"],
        "share_count": shares.get("total") or 0,
        "dead_shares": shares.get("dead") or 0,
        "queue": {x: counts.get(x, 0) for x in ("pending", "running", "success", "failed")},
        "last_run": latest["time"],
        "last_result": latest["result"],
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
:root{--bg:#f5f7fb;--card:#fff;--ink:#172033;--blue:#2364d2;--ok:#138a4b;--bad:#c83737;--warn:#aa6900}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px system-ui,-apple-system,"Segoe UI",sans-serif}header{background:#14213d;color:white;padding:13px max(16px,calc((100% - 1000px)/2));display:flex;gap:12px;align-items:center;justify-content:space-between}nav{display:flex;gap:12px;flex-wrap:wrap}a{color:var(--blue);text-decoration:none}header a{color:#fff}.container{max-width:1000px;margin:auto;padding:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px}.card,form,.tablewrap{background:var(--card);border-radius:10px;padding:15px;box-shadow:0 1px 3px #0001;margin-bottom:14px}.metric{font-size:25px;font-weight:700}.label{color:#657085;font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}button,.button{border:0;border-radius:7px;background:var(--blue);color:#fff;padding:10px 13px;font:inherit;cursor:pointer}button.secondary{background:#657085}button.danger{background:var(--bad)}input{width:100%;padding:9px;border:1px solid #cbd3e1;border-radius:6px;font:inherit}label{display:block;margin:9px 0 4px;font-weight:600}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid #e7eaf0;vertical-align:top}.url{word-break:break-all;overflow-wrap:anywhere}.tablewrap{overflow-x:auto}pre.log{white-space:pre-wrap;overflow-wrap:anywhere;font:12px ui-monospace,SFMono-Regular,monospace;margin:0}.event{padding:8px 0;border-bottom:1px solid #e7eaf0}.tag{font-size:12px;font-weight:bold;padding:2px 5px;border-radius:4px;background:#e8eefc}.flash{padding:10px;background:#e5f7eb;border-radius:7px;margin-bottom:12px}.actions{display:flex;gap:8px;flex-wrap:wrap}.muted{color:#657085}@media(max-width:560px){header{align-items:flex-start;flex-direction:column}th,td{padding:7px 5px}.container{padding:10px}}</style><body><header><strong>quark-follow</strong><nav><a href='/'>概览</a><a href='/resources'>资源</a><a href='/logs'>日志</a><a href='/config'>配置</a><a href='/logout'>退出</a></nav></header><main class='container'>{% with messages=get_flashed_messages() %}{% for m in messages %}<div class='flash'>{{m}}</div>{% endfor %}{% endwith %}"""

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
        BASE_TEMPLATE + """<h1>系统概览</h1><div id='dashboard'></div><p class='muted'>页面每 3 秒刷新；运行状态根据实际锁目录及进程检测。</p><script>const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));async function load(){let d=await fetch('/api/dashboard').then(r=>r.json());let t=d.tasks.resource_check;let q=d.queue;document.querySelector('#dashboard').innerHTML=`<div class=grid><div class=card><div class=label>resource_check</div><div class="metric ${t.running?'warn':'ok'}">${t.running?'运行中':'空闲'}</div><div class=label>${t.pid?'PID '+t.pid:'无进程'}</div></div><div class=card><div class=label>剧集数量</div><div class=metric>${d.show_count}</div></div><div class=card><div class=label>Share / dead</div><div class=metric>${d.share_count} / ${d.dead_shares}</div></div><div class=card><div class=label>替换队列</div><div>pending ${q.pending} · running ${q.running}<br>success ${q.success} · failed ${q.failed}</div></div></div><div class=card><div class=label>最近一次 resource_check</div>${d.last_run||'暂无'} · ${d.last_result}<div class=actions style="margin-top:12px"><form method=post action='/tasks/resource-check'><button ${t.running?'disabled':''}>立即运行 resource_check</button></form></div></div><section class=card><h2>最近事件</h2>${d.events.map(e=>`<div class=event><span class=tag>${e.category}</span> <span class=muted>${esc(e.time)} ${esc(e.source)}</span><br>${esc(e.message)}</div>`).join('')||'暂无日志'}</section>`}load();setInterval(load,3000)</script></main>"""
    )

@app.route('/api/dashboard')
@login_required
def api_dashboard():
    return jsonify(dashboard_data())

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
        BASE_TEMPLATE + """<h1>资源</h1><div class=actions><a class=button href='/resources/add'>添加资源</a><a class='button secondary' href='/resources/excluded'>排除列表</a></div><div class=tablewrap><table><tr><th>名称</th><th>总/拥有/缺失</th><th>Share</th><th>状态</th><th>WebDAV 路径</th></tr>{% for r in rows %}<tr><td><a href='/resources/{{r.id}}'>{{r.name}}</a></td><td>{{r.total_episodes or 0}} / {{r.owned or 0}} / {{r.missing}}</td><td>{{r.share_count}}</td><td>{{'已完成' if r.missing == 0 else '待补集'}}</td><td>{{r.webdav_path}}</td></tr>{% else %}<tr><td colspan=5>数据库暂无资源。</td></tr>{% endfor %}</table></div></main>""",
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
    owned = {r['episode'] for r in db_query(
        "SELECT DISTINCT episode FROM webdav_files WHERE show_id=?",
        (show_id,),
    )}
    total = show['total_episodes'] or 0
    missing = [x for x in range(1, total + 1) if x not in owned]
    return render_template_string(
        BASE_TEMPLATE + """<h1>{{show.name}}</h1><div class=card><p><b>SeedHub：</b><a href='{{show.seedhub_url}}' rel=noopener>{{show.seedhub_url}}</a></p><p><b>WebDAV：</b>{{show.webdav_path}}</p><p>总集数 {{total}} · 当前集数 {{owned|length}} · 缺失 {{missing|length}}</p><p class=muted>缺失集：{{ missing|join(', ') if missing else '无' }}</p><div class=actions><form method=post action='/tasks/source-check/show/{{show.id}}'><button>重新扫描此资源</button></form><form method=post action='/resources/{{show.id}}/delete' onsubmit="return confirm('确定删除此资源？\\n\\n将删除数据库中的资源及缓存，并从 resources.json 中移除追踪记录。\\nWebDAV 中已经存在的文件不会删除。\\n此操作不可撤销。');"><button type=submit class=danger>删除资源</button></form></div></div><div class=tablewrap><table><tr><th>rank</th><th>share id</th><th>Share URL</th><th>状态</th><th>失败</th><th>集数统计</th><th>操作</th></tr>{% for s in shares %}<tr><td>{{s.seedhub_rank}}</td><td>{{s.id}}</td><td><a class=url href='{{s.url}}' rel=noopener>{{s.url}}</a></td><td>{{s.status}}</td><td>{{s.fail_count}}</td><td>{{s.episode_count}} 集 / {{s.file_count}} 文件</td><td>{% if s.status == 'excluded' %}<span class=bad>已排除</span>{% else %}<form method=post action='/tasks/source-check/share/{{s.id}}'><button>重扫</button></form><form method=post action='/resources/{{show.id}}/shares/{{s.id}}/exclude'><button type=submit class=danger>排除</button></form>{% endif %}</td></tr>{% endfor %}</table></div></main>""",
        show=show,
        shares=shares,
        total=total,
        owned=owned,
        missing=missing,
    )


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
            f"Share 排除失败：show_id={show_id} share_id={share_id} error={exc}",
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
            f"资源删除失败：show_id={show_id} name={show['name']} error={exc}",
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
    return start_task('resource_check.sh', [])

@app.post('/tasks/source-check/<kind>/<int:target_id>')
@login_required
def start_source_check(kind, target_id):
    if kind not in ('show','share'):
        abort(404)
    if task_status()['resource_check']['running'] or process_for('source_check.sh'):
        flash('已有资源检查或 Share 扫描任务在运行。')
        return redirect(request.referrer or url_for('index'))
    return start_task('source_check.sh', [f'--{kind}-id', str(target_id)])

def start_task(script, args):
    with launch_lock:
        state = task_status()
        if state['resource_check']['running'] or (script == 'replace.sh' and state['replace']['running']):
            flash('检测到冲突锁或运行进程，未启动重复任务。')
        elif not (BASE / script).is_file():
            flash('脚本不存在，未启动。')
        else:
            log = LOG_DIR / 'web_launcher.log'
            LOG_DIR.mkdir(exist_ok=True)
            with log.open('ab') as output:
                subprocess.Popen(
                    [str(BASE / script), *args],
                    cwd=BASE,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            flash(f'已在后台启动 {script}。')
    return redirect(request.referrer or url_for('index'))

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
