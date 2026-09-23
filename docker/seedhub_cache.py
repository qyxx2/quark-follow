#!/usr/bin/env python3

import argparse
import os
import re
import sys
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import builtins
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

os.environ.setdefault("QUARK_FOLLOW_BASE", "/data")
try:
    from api_stats import record_event as _api_stats_record_event
    from api_stats import flush_event_file as _api_stats_flush_event_file
except Exception:
    def _api_stats_record_event(*_args, **_kwargs):
        return None

    def _api_stats_flush_event_file(*_args, **_kwargs):
        return False


# ============================================================
# 基本配置
# ============================================================

BASE_URL = "https://www.seedhub.cc"
DB_FILE = "/data/resource.db"
LOG_FILE = Path("/data/logs/seedhub_cache.log")
API_STATS_FILE = LOG_FILE.parent / f"api_stats_live_seedhub_{os.getpid()}.tsv"

FRONT_SHARE_LIMIT = 20
PAGE_TIMEOUT = 60000
PAGE_DELAY = 2

# 已有二级入口低频重新确认：
# - 每个入口默认至少 72 小时才重新打开一次；
# - 每次运行最多确认 2 个已有入口；
# - 新进入前20的二级入口仍按原逻辑立即打开，不受此限制。
DEFAULT_ENTRY_RECHECK_HOURS = 72
DEFAULT_ENTRY_RECHECK_MAX = 2


def env_nonnegative_int(name, default):
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"配置 {name}={raw!r} 无效，使用默认值 {default}")
        return default
    return max(0, value)


ENTRY_RECHECK_HOURS = env_nonnegative_int(
    "RESOURCE_SEEDHUB_ENTRY_RECHECK_HOURS",
    DEFAULT_ENTRY_RECHECK_HOURS,
)
ENTRY_RECHECK_MAX = env_nonnegative_int(
    "RESOURCE_SEEDHUB_ENTRY_RECHECK_MAX",
    DEFAULT_ENTRY_RECHECK_MAX,
)

CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

ROMAN_SEASONS = {
    "Ⅰ": 1,
    "Ⅱ": 2,
    "Ⅲ": 3,
    "Ⅳ": 4,
    "Ⅴ": 5,
    "Ⅵ": 6,
    "Ⅶ": 7,
    "Ⅷ": 8,
    "Ⅸ": 9,
    "Ⅹ": 10,
}


def api_stats_record(operation, failed=False):
    try:
        _api_stats_record_event(API_STATS_FILE, "seedhub", operation, failed=failed)
    except Exception:
        pass


def api_stats_flush():
    try:
        _api_stats_flush_event_file(API_STATS_FILE)
    except Exception:
        pass


LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
try:
    API_STATS_FILE.touch(exist_ok=True)
    os.chmod(API_STATS_FILE, 0o600)
except OSError:
    pass


def log_print(*args, **kwargs):
    builtins.print(*args, **kwargs)
    separator = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    message = separator.join(str(arg) for arg in args)
    if message:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [PARSE] {message}{end}"
            )


print = log_print


# ============================================================
# 文本处理
# ============================================================

def normalize_text(text):
    if not text:
        return ""

    text = text.replace("\r", "\n")

    text = re.sub(
        r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])',
        '',
        text
    )

    text = re.sub(r'[ \t]+', ' ', text)

    lines = []

    for line in text.splitlines():
        line = line.strip()

        if line:
            lines.append(line)

    return "\n".join(lines)


# ============================================================
# 季数解析
#
# 一级页面才负责确定当前季。
# 不读取 body 中的普通文本，因为正文可能同时提到其它季。
# ============================================================

def parse_number_token(value):
    value = (value or "").strip().replace("〇", "零").replace("兩", "两")

    if value.isdigit():
        return int(value)

    if value in CN_DIGITS:
        return CN_DIGITS[value]

    if all(ch in CN_DIGITS for ch in value):
        number = 0
        for ch in value:
            number = number * 10 + CN_DIGITS[ch]
        return number

    if "百" in value:
        left, right = value.split("百", 1)
        hundreds = 1 if not left else parse_number_token(left)
        remainder = parse_number_token(right) if right else 0
        return hundreds * 100 + remainder

    if "十" in value:
        left, right = value.split("十", 1)
        tens = 1 if not left else parse_number_token(left)
        remainder = parse_number_token(right) if right else 0
        return tens * 10 + remainder

    raise ValueError(f"无法解析数字：{value}")


def season_markers(text):
    if not text:
        return []

    patterns = [
        r'第\s*([0-9]+|[零〇兩两一二三四五六七八九十百]+)\s*季',
        r'\bSeason\s*([0-9]{1,2})\b',
        r'(?<![A-Za-z0-9])[Ss]([0-9]{1,2})(?![A-Za-z0-9])',
    ]

    found = []

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            number = parse_number_token(match.group(1))
            if number >= 1:
                found.append(number)

    return found


def detect_season(page, movie_url, primary_title):
    """
    先判断一级页面主标题；主标题没有季号时，再看 title / URL。
    没有明确季号时按第1季处理。
    """
    primary_found = season_markers(primary_title)
    primary_unique = sorted(set(primary_found))

    if len(primary_unique) > 1:
        raise ValueError(
            f"一级 SeedHub 主标题季度信号冲突：{primary_title!r} -> "
            + ", ".join(f"第{season}季" for season in primary_unique)
        )

    if len(primary_unique) == 1:
        return primary_unique[0]

    sources = []

    try:
        title = normalize_text(page.title())
        if title and title != primary_title:
            sources.append(("title", title))
    except Exception:
        pass

    sources.append(("url", movie_url))

    found = []
    for source, value in sources:
        for season in season_markers(value):
            found.append((season, source, value))

    unique = sorted({season for season, _, _ in found})

    if not unique:
        return 1

    if len(unique) != 1:
        details = "; ".join(
            f"{source}={value!r}->第{season}季"
            for season, source, value in found
        )
        raise ValueError(f"一级 SeedHub 补充季度信号冲突：{details}")

    return unique[0]


def remove_season_markers(text):
    text = text or ""

    text = re.sub(
        r'第\s*(?:[0-9]+|[零〇一二两三四五六七八九十百]+)\s*季',
        '',
        text,
        flags=re.I
    )

    text = re.sub(
        r'(?<![A-Za-z0-9])Season\s*[0-9]{1,2}(?![0-9])',
        '',
        text,
        flags=re.I
    )

    text = re.sub(
        r'(?<![A-Za-z0-9])[Ss]\s*[0-9]{1,2}(?![0-9])',
        '',
        text,
        flags=re.I
    )

    text = re.sub(
        r'시즌\s*[0-9]{1,2}',
        '',
        text,
        flags=re.I
    )

    text = re.sub(
        r'[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\s*$',
        '',
        text
    )

    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = text.strip(' -–—_|｜')

    return text.strip()


def seasonize_show_name(show_name, season):
    """
    生成统一的短资源名称。

    优先保留一级页面中的中文剧名及其中文季度写法，例如：
        绅士们第二季 The Gentlemen Season 2
        -> 绅士们第二季

    如果中文标题本身没有季度标记，则在中文标题后补“第N季”。
    纯英文标题则去掉英文 Season/SN 季数标记后补“第N季”。
    """
    text = normalize_text(show_name)

    if not text:
        return ""

    # 页面可能把站点名称拼在标题尾部。
    text = re.sub(
        r'\s*[-|｜]\s*SeedHub.*$',
        '',
        text,
        flags=re.I
    ).strip()

    # 最优先：中文标题自身已经带“第N季”。
    chinese_season = re.match(
        r'^(.*?第\s*(?:[0-9]+|[零〇一二两三四五六七八九十百]+)\s*季)',
        text,
        re.I
    )
    if chinese_season:
        base = normalize_text(chinese_season.group(1))
        base = re.sub(r'\s+', '', base)
        if re.search(r'[\u4e00-\u9fff]', base):
            return base

    # 中文标题没有中文季度标记，但后面带英文片名/英文 Season 标记时，
    # 只保留中文标题，避免把整段英文片名写入 WebDAV 目录。
    chinese_match = re.search(
        r'[\u4e00-\u9fff][\u4e00-\u9fff\s·・\-—_]*',
        text
    )
    if chinese_match:
        chinese_base = re.sub(r'\s+', '', chinese_match.group(0)).strip(' -–—_|｜')
        if chinese_base:
            # 没有显式季度标记的中文单季资源保持原剧名，不人为追加“第1季”。
            if season == 1:
                return chinese_base
            return f"{chinese_base} 第{season}季"

    # 没有中文名时保留英文片名，但去掉已有 Season/SN 季数标记。
    base = remove_season_markers(text)
    if not base:
        base = text.strip()
    return f"{base} 第{season}季"


def get_show_name(page):
    h1_candidates = []

    try:
        h1s = page.locator("h1").all_inner_texts()

        for raw in h1s:
            text = normalize_text(raw)

            if not text:
                continue

            lines = []

            for line in text.splitlines():
                line = line.strip()

                if not line:
                    continue

                if line == "#":
                    continue

                line = re.sub(
                    r'^#\s*',
                    '',
                    line
                ).strip()

                if line:
                    lines.append(line)

            if lines:
                h1_candidates.append(lines[0])
    except Exception:
        pass

    source_name = ""

    if h1_candidates:
        source_name = h1_candidates[0]

    if not source_name:
        try:
            source_name = normalize_text(page.title())

            source_name = re.sub(
                r'\s*[-|｜]\s*SeedHub.*$',
                '',
                source_name,
                flags=re.I
            )

            source_name = re.sub(
                r'^#\s*',
                '',
                source_name
            ).strip()
        except Exception:
            source_name = ""

    if not source_name:
        return ""

    return source_name


# ============================================================
# 获取真实总集数
# ============================================================

def detect_total_episodes(page):
    try:
        body_text = page.locator("body").inner_text()
        body_text = normalize_text(body_text)

        patterns = [
            r'集\s*数\s*[:：]\s*(\d+)',
            r'集数\s*[:：]\s*(\d+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, body_text, re.I)

            if match:
                value = int(match.group(1))

                if value > 0:
                    return value

    except Exception:
        pass

    return 0


# ============================================================
# 获取一级页面中的 Quark 入口
# ============================================================

def get_quark_entries(page, start_rank, max_count):
    links = page.locator(
        '.pan-links a[data-link*="quark"]'
    )

    total = links.count()

    print("找到夸克入口:", total)

    if start_rank > total:
        return [], total

    end_rank = min(
        total,
        start_rank + max_count - 1
    )

    items = []

    for rank in range(start_rank, end_rank + 1):
        a = links.nth(rank - 1)

        href = a.get_attribute("href")

        title = normalize_text(
            a.inner_text()
        )

        if not href:
            continue

        items.append({
            "rank": rank,
            "title": title,
            "url": urljoin(BASE_URL, href)
        })

    return items, total


# ============================================================
# 从二级页面提取最终 Quark URL
# ============================================================

def extract_quark_url(html):
    found = re.findall(
        r'https?://pan\.quark\.cn/[^\s"\'<>]+',
        html
    )

    found = list(dict.fromkeys(found))

    if not found:
        return ""

    return found[0]


# ============================================================
# 数据库：获取 / 创建 show
# ============================================================

def replace_webdav_leaf(path, name):
    path = (path or "").rstrip("/")

    if not path:
        return "/kuake/其他/" + name

    parent = path.rsplit("/", 1)[0]

    if not parent:
        return "/" + name

    return parent + "/" + name


def get_pwd_id(quark_url):
    match = re.search(r"https?://pan\.quark\.cn/s/([^/#?]+)", quark_url or "", re.I)
    return match.group(1) if match else ""


def is_blacklisted(conn, show_id, quark_url):
    pwd_id = get_pwd_id(quark_url)
    if not pwd_id:
        return False

    row = conn.execute(
        "SELECT 1 FROM share_blacklist WHERE show_id=? AND pwd_id=? LIMIT 1",
        (show_id, pwd_id),
    ).fetchone()

    return row is not None


def ensure_column(conn, table, column, definition):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        print(f"数据库迁移：新增 {table}.{column}")
        return True
    return False


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS share_blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_id INTEGER NOT NULL,
            pwd_id TEXT NOT NULL,
            url TEXT NOT NULL,
            seedhub_entry_url TEXT,
            reason TEXT NOT NULL DEFAULT 'empty_after_3_scans',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE,
            UNIQUE(show_id,pwd_id)
        )
    """)

    ensure_column(conn, "shows", "share_scan_rank", "INTEGER DEFAULT 0")
    ensure_column(conn, "shows", "discovery_cursor", "INTEGER NOT NULL DEFAULT 20")
    ensure_column(conn, "shows", "discovery_fail_rank", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "shows", "discovery_fail_count", "INTEGER NOT NULL DEFAULT 0")
    new_pool_type = ensure_column(conn, "shares", "pool_type", "TEXT NOT NULL DEFAULT 'front20'")
    ensure_column(conn, "shares", "seedhub_entry_url", "TEXT")
    ensure_column(conn, "shares", "seedhub_entry_checked_at", "TEXT")
    ensure_column(conn, "shares", "pending_probe_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "shares", "used_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "shares", "last_used_at", "TEXT")

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_share_blacklist_show_pwd ON share_blacklist(show_id,pwd_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_show_pool_rank ON shares(show_id,pool_type,seedhub_rank)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_show_entry ON shares(show_id,seedhub_entry_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shares_show_entry_checked ON shares(show_id,seedhub_entry_checked_at)")

    # 旧版本可能把“成功但没有任何 share_files”的 Share 留成 valid。
    # 它们已经至少经历过一次成功扫描，因此从 pending_probe_count=1 开始。
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


def get_or_create_show(
    conn,
    show_name,
    movie_url,
    webdav_path,
    total_episodes
):
    conn.execute(
        """
        INSERT INTO shows
        (
            name,
            seedhub_url,
            webdav_path,
            total_episodes,
            latest_episode,
            share_scan_rank
        )
        VALUES (?, ?, ?, ?, 0, 0)

        ON CONFLICT(seedhub_url)
        DO UPDATE SET

            name = excluded.name,

            webdav_path = excluded.webdav_path,

            total_episodes =
                CASE
                    WHEN excluded.total_episodes > 0
                    THEN excluded.total_episodes
                    ELSE shows.total_episodes
                END,

            updated_at = CURRENT_TIMESTAMP
        """,
        (
            show_name,
            movie_url,
            webdav_path,
            total_episodes
        )
    )

    row = conn.execute(
        """
        SELECT
            id,
            total_episodes,
            share_scan_rank,
            webdav_path
        FROM shows
        WHERE seedhub_url = ?
        """,
        (
            movie_url,
        )
    ).fetchone()

    return row


# ============================================================
# 数据库：写入 share
# ============================================================

def save_share(
    conn,
    show_id,
    quark_url,
    rank,
    seedhub_entry_url,
    pool_type
):
    conn.execute(
        """
        INSERT INTO shares
        (
            show_id,
            url,
            seedhub_entry_url,
            seedhub_rank,
            status,
            pool_type,
            fail_count
        )
        VALUES (?, ?, ?, ?, 'unknown', ?, 0)

        ON CONFLICT(show_id, url)
        DO UPDATE SET
            seedhub_entry_url = CASE
                WHEN excluded.pool_type='front20'
                     AND (shares.seedhub_rank IS NULL OR excluded.seedhub_rank <= shares.seedhub_rank)
                THEN excluded.seedhub_entry_url
                WHEN shares.seedhub_entry_url IS NULL
                THEN excluded.seedhub_entry_url
                ELSE shares.seedhub_entry_url
            END,
            seedhub_rank = CASE
                WHEN shares.seedhub_rank IS NULL THEN excluded.seedhub_rank
                WHEN excluded.pool_type='front20' AND excluded.seedhub_rank < shares.seedhub_rank THEN excluded.seedhub_rank
                ELSE shares.seedhub_rank
            END,
            pool_type = CASE
                WHEN shares.pool_type='front20' OR excluded.pool_type='front20' THEN 'front20'
                ELSE 'overflow'
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            show_id,
            quark_url,
            seedhub_entry_url,
            rank,
            pool_type
        )
    )


# ============================================================
# SeedHub 前20固定资源池 / 受控额外探索
# ============================================================

def checked_at_is_due(value):
    if not value:
        return True

    try:
        checked_at = datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True

    elapsed = (
        datetime.now(timezone.utc) - checked_at
    ).total_seconds()

    return elapsed >= ENTRY_RECHECK_HOURS * 3600


def mark_entry_checked(conn, share_id):
    conn.execute(
        "UPDATE shares SET seedhub_entry_checked_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (share_id,),
    )


def refresh_existing_front20_entry(
    conn,
    page,
    show_id,
    item,
    existing
):
    """
    低频重新确认已有二级入口。

    这里故意不改变原有 Share 生命周期：
    - 只在二级页成功访问后更新时间；
    - 页面暂时没有 Quark 时保留旧 Share，避免一次异常把旧资源删除；
    - 如果 Quark URL 真正变化，则把新 URL 写入 shares；
    - 原有 Share 不直接删除，让 resource_check/source_check 的现有缓存和替换逻辑继续工作。
    """
    entry_url = item["url"]
    old_share_id = existing["id"]
    old_quark_url = existing.get("url") or ""

    print()
    print(
        "[%02d] 低频确认已有二级入口：%s"
        % (item["rank"], item["title"][:100])
    )
    print("     二级:", entry_url)
    print(
        "     上次确认:",
        existing.get("seedhub_entry_checked_at") or "从未确认"
    )

    try:
        quark_url = open_entry_and_extract(page, item)
    except Exception as exc:
        print("     二级入口重新确认失败:", exc)
        return False

    # 页面已经成功打开。即使暂时没抓到 Quark，也记录确认时间，
    # 防止异常页面连续触发 Playwright 请求。
    mark_entry_checked(conn, old_share_id)

    if not quark_url:
        print("     本次未找到 Quark，保留原 Share：", old_quark_url or "<empty>")
        conn.commit()
        return True

    if old_quark_url == quark_url:
        print("     Quark 未变化：", quark_url)
        conn.commit()
        return True

    print("     检测到 Quark URL 变化：")
    print("       旧:", old_quark_url or "<empty>")
    print("       新:", quark_url)

    if is_blacklisted(conn, show_id, quark_url):
        print("     新 Quark 位于黑名单，保留原 Share，不覆盖旧缓存：", quark_url)
        conn.commit()
        return True
        conn.commit()
        return True

    before = conn.execute(
        """
        SELECT id, seedhub_entry_url, seedhub_rank
        FROM shares
        WHERE show_id=? AND url=?
        LIMIT 1
        """,
        (show_id, quark_url),
    ).fetchone()

    if before is None:
        save_share(
            conn,
            show_id,
            quark_url,
            item["rank"],
            entry_url,
            "front20",
        )
        after = conn.execute(
            "SELECT id FROM shares WHERE show_id=? AND url=? LIMIT 1",
            (show_id, quark_url),
        ).fetchone()
        new_share_id = after[0] if after else None
    else:
        new_share_id = before[0]
        save_share(
            conn,
            show_id,
            quark_url,
            item["rank"],
            entry_url,
            "front20",
        )

    if new_share_id:
        # 新 Share 如果本来就是该入口对应的 Share，则只需刷新确认时间。
        # 如果新 URL 已存在于其它入口，则沿用原有“较早 rank 优先”规则；
        # 只有当前入口的 rank 不低于旧映射时，才接管该映射。
        new_row = conn.execute(
            """
            SELECT id,seedhub_entry_url,seedhub_rank
            FROM shares
            WHERE id=?
            LIMIT 1
            """,
            (new_share_id,),
        ).fetchone()

        can_claim = bool(
            new_row
            and (
                new_row[1] == entry_url
                or not new_row[1]
                or (
                    new_row[2] is not None
                    and int(new_row[2]) >= int(item["rank"])
                )
            )
        )

        if can_claim:
            previous_entry = new_row[1] if new_row else None
            if new_share_id != old_share_id and previous_entry and previous_entry != entry_url:
                # 新 URL 原先属于另一个入口；把那个入口释放出来，
                # 后续完整前20同步仍会按当前一级页面重新确认其映射。
                conn.execute(
                    """
                    UPDATE shares
                       SET seedhub_entry_url=NULL,
                           pool_type='overflow',
                           updated_at=CURRENT_TIMESTAMP
                     WHERE id=?
                    """,
                    (new_share_id,),
                )

            conn.execute(
                """
                UPDATE shares
                   SET seedhub_entry_url=?,
                       seedhub_rank=?,
                       pool_type='front20',
                       seedhub_entry_checked_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE id=?
                """,
                (entry_url, item["rank"], new_share_id),
            )

            if new_share_id != old_share_id:
                # 旧 Quark URL 保留及其 share_files 均不删除，只解除当前二级入口映射。
                conn.execute(
                    """
                    UPDATE shares
                       SET seedhub_entry_url=NULL,
                           pool_type='overflow',
                           updated_at=CURRENT_TIMESTAMP
                     WHERE id=?
                    """,
                    (old_share_id,),
                )

            print("     已将当前二级入口映射到新 Share：share_id=", new_share_id)
        else:
            # 新 URL 已由更高优先级入口占用：不抢占已有映射，也不删除旧 Share。
            print(
                "     新 Share 已存在且保留更高优先级入口映射，保留旧入口映射：share_id=",
                new_share_id,
            )

    # 旧 Share 不删除、不清理 share_files；只有在新 URL 成功接管当前入口时才解除映射。
    conn.commit()
    return True


def open_entry_and_extract(page, item):
    print()
    print("[%02d] %s" % (item["rank"], item["title"][:100]))
    print("     二级:", item["url"])

    try:
        page.goto(
            item["url"],
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )
        html = page.content()
    except Exception:
        api_stats_record("share_page", failed=True)
        raise
    else:
        api_stats_record("share_page", failed=False)

    quark_url = extract_quark_url(html)
    if quark_url:
        print("     Quark:", quark_url)
    else:
        print("     没找到 Quark")
    return quark_url


def reconcile_front20(conn, page, show_id):
    items, total_links = get_quark_entries(page, 1, FRONT_SHARE_LIMIT)
    current_entries = {item["url"] for item in items}
    existing_by_entry = {}
    for row in conn.execute(
        """
        SELECT id,seedhub_entry_url,url,seedhub_entry_checked_at,seedhub_rank
        FROM shares
        WHERE show_id=? AND seedhub_entry_url IS NOT NULL
        ORDER BY COALESCE(seedhub_rank,999999),id
        """,
        (show_id,),
    ).fetchall():
        entry_url = row[1]
        if entry_url in existing_by_entry:
            continue
        existing_by_entry[entry_url] = {
            "id": row[0],
            "seedhub_entry_url": row[1],
            "url": row[2],
            "seedhub_entry_checked_at": row[3],
            "seedhub_rank": row[4],
        }

    fetched = 0
    added = 0
    failed = 0
    existing_recheck_due = []

    for item in items:
        entry_url = item["url"]
        existing = existing_by_entry.get(entry_url)

        if existing is not None:
            conn.execute(
                "UPDATE shares SET seedhub_rank=?, pool_type='front20', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (item["rank"], existing["id"]),
            )

            # 注意：这里不重新打开二级页。
            # 只有进入低频确认冷却窗口的已有入口才加入本轮确认队列。
            if (
                ENTRY_RECHECK_MAX > 0
                and checked_at_is_due(existing.get("seedhub_entry_checked_at"))
            ):
                existing_recheck_due.append((item, existing))
            continue

        try:
            quark_url = open_entry_and_extract(page, item)
            fetched += 1
        except Exception as exc:
            print("     ERROR:", exc)
            failed += 1
            continue

        if not quark_url:
            continue

        if is_blacklisted(conn, show_id, quark_url):
            print("     黑名单跳过:", quark_url)
            continue

        before = conn.execute(
            "SELECT id,seedhub_entry_url,seedhub_rank FROM shares WHERE show_id=? AND url=? LIMIT 1",
            (show_id, quark_url),
        ).fetchone()
        if before is None:
            save_share(conn, show_id, quark_url, item["rank"], entry_url, "front20")
            added += 1
            share_id = conn.execute(
                "SELECT id FROM shares WHERE show_id=? AND url=? LIMIT 1",
                (show_id, quark_url),
            ).fetchone()[0]
        else:
            share_id = before[0]
            # The current first-20 page is authoritative for the active mapping.
            # If the same Quark URL used to belong to an entry that is no longer in
            # the current first 20, move that Share to the new entry/rank. If the old
            # entry is still in the current first 20, keep its earlier rank.
            if before[1] not in current_entries or item["rank"] < (before[2] or 999999):
                conn.execute(
                    "UPDATE shares SET seedhub_entry_url=?,seedhub_rank=?,pool_type='front20',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (entry_url, item["rank"], share_id),
                )
            else:
                conn.execute(
                    "UPDATE shares SET pool_type='front20',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (share_id,),
                )
        conn.execute(
            "UPDATE shares SET seedhub_entry_checked_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (share_id,),
        )
        existing_by_entry[entry_url] = {
            "id": share_id,
            "seedhub_entry_url": entry_url,
            "url": quark_url,
            "seedhub_entry_checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "seedhub_rank": item["rank"],
        }

        if item is not items[-1]:
            time.sleep(PAGE_DELAY)

    # 只有在一级页面的 Quark 入口集合完整可判定时，才允许把旧 front20 降级。
    # 如果页面标记的总入口数量大于本次实际解析出的入口数量，说明本次结果可能不完整；
    # 此时保留旧 front20，避免一次临时解析异常误删/降级正常资源。
    complete_front20 = total_links > 0 and len(items) == min(total_links, FRONT_SHARE_LIMIT)

    # 本轮只确认少量已有二级入口。
    # 按前20 rank 顺序处理，保证行为稳定；每个入口仍遵守自己的冷却时间。
    checked_existing = 0
    if ENTRY_RECHECK_MAX > 0 and existing_recheck_due:
        print()
        print(
            "开始低频确认已有二级入口：",
            f"候选={len(existing_recheck_due)}",
            f"本轮上限={ENTRY_RECHECK_MAX}",
            f"冷却={ENTRY_RECHECK_HOURS}小时",
        )

        for item, existing in existing_recheck_due:
            if checked_existing >= ENTRY_RECHECK_MAX:
                break

            if refresh_existing_front20_entry(
                conn,
                page,
                show_id,
                item,
                existing,
            ):
                checked_existing += 1

            if checked_existing < min(
                ENTRY_RECHECK_MAX,
                len(existing_recheck_due)
            ):
                time.sleep(PAGE_DELAY)

    if complete_front20 and current_entries:
        placeholders = ",".join("?" for _ in current_entries)
        params = [show_id, *current_entries]
        conn.execute(
            f"UPDATE shares SET pool_type='overflow', updated_at=CURRENT_TIMESTAMP "
            f"WHERE show_id=? AND pool_type='front20' AND "
            f"(seedhub_entry_url IS NULL OR seedhub_entry_url NOT IN ({placeholders}))",
            params,
        )
    elif complete_front20:
        conn.execute(
            "UPDATE shares SET pool_type='overflow', updated_at=CURRENT_TIMESTAMP WHERE show_id=? AND pool_type='front20'",
            (show_id,),
        )
    else:
        print("一级页面入口解析不完整，保留现有 front20 资源池，不执行旧 Share 降级。")

    # 原来的前20同步只更新 rank/cursor；
    # 这里额外保留低频确认的统计信息，不改变其原有字段语义。
    conn.execute(
        "UPDATE shows SET share_scan_rank=?, discovery_cursor=CASE WHEN COALESCE(discovery_cursor,0)<? THEN ? ELSE discovery_cursor END, last_scan=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (min(total_links, FRONT_SHARE_LIMIT), FRONT_SHARE_LIMIT, FRONT_SHARE_LIMIT, show_id),
    )
    conn.commit()

    print()
    print("前20资源池同步完成：")
    print("  SeedHub Quark 入口:", total_links)
    print("  当前前20入口:", len(items))
    print("  新解析二级入口:", fetched)
    print("  新增/复用 Share:", added, "/", len(items))
    print("  已确认旧二级入口:", checked_existing)
    print("  待低频确认旧入口:", max(0, len(existing_recheck_due) - checked_existing))
    print("  二级入口失败:", failed)
    print("  share_scan_rank:", min(total_links, FRONT_SHARE_LIMIT))


def scan_seedhub_range(conn, page, show_id, start_rank, count):
    items, total_links = get_quark_entries(page, start_rank, count)
    if not items:
        print(f"请求的 SeedHub rank={start_rank} 不存在，当前总入口={total_links}")
        return 3

    processed = 0
    failed = 0
    current_cursor = conn.execute(
        "SELECT COALESCE(discovery_cursor,20) FROM shows WHERE id=?", (show_id,)
    ).fetchone()[0]
    current_cursor = max(int(current_cursor or 20), FRONT_SHARE_LIMIT)

    for index, item in enumerate(items):
        rank = item["rank"]
        try:
            quark_url = open_entry_and_extract(page, item)
        except Exception as exc:
            print("     ERROR:", exc)
            failed += 1
            break

        if not quark_url:
            print("     未找到 Quark URL，本次不推进 discovery_cursor，交由外层失败计数控制重试。")
            failed += 1
            break

        processed += 1
        if not is_blacklisted(conn, show_id, quark_url):
            save_share(conn, show_id, quark_url, rank, item["url"], "overflow")
        else:
            print("     黑名单跳过:", quark_url)

        current_cursor = max(current_cursor, rank)
        conn.execute(
            "UPDATE shows SET discovery_cursor=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (current_cursor, show_id),
        )
        conn.commit()

        if index < len(items)-1:
            time.sleep(PAGE_DELAY)

    if failed:
        return 1

    conn.execute(
        "UPDATE shows SET last_scan=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (show_id,),
    )
    conn.commit()
    print(f"受控探索完成：rank={start_rank}-{start_rank+processed-1} cursor={current_cursor}")
    return 0


# ============================================================
# 主程序
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="SeedHub Share cache")
    parser.add_argument("movie_url")
    parser.add_argument("--range", dest="range_args", nargs=2, type=int, metavar=("START_RANK", "COUNT"))
    args = parser.parse_args()

    movie_url = args.movie_url

    if args.range_args and (args.range_args[0] < 1 or args.range_args[1] < 1):
        print("错误：--range 的 start_rank 和 count 必须 >= 1", file=sys.stderr)
        sys.exit(2)

    if not re.match(
        r'^https?://(?:www\.)?seedhub\.cc/movies/',
        movie_url,
        re.I
    ):
        print(
            "错误：不是有效的 SeedHub 电影/剧集页面 URL",
            file=sys.stderr
        )
        sys.exit(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False
        )

        page = browser.new_page(
            viewport={
                "width": 1920,
                "height": 1080
            }
        )

        print("打开电影页面...")
        print(movie_url)

        try:
            page.goto(
                movie_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )
        except Exception as e:
            api_stats_record("movie_page", failed=True)
            print(
                "一级页面打开失败:",
                e,
                file=sys.stderr
            )
            browser.close()
            api_stats_flush()
            sys.exit(1)
        else:
            api_stats_record("movie_page", failed=False)

        # ----------------------------------------------------
        # 解析当前一级页面的主标题 + 季数
        # 季数必须在后续剧名、目录名、数据库 show 信息生成之前确定。
        # ----------------------------------------------------

        show_name_raw = get_show_name(page)

        if not show_name_raw:
            print(
                "错误：无法从一级页面获取剧集名称",
                file=sys.stderr
            )
            browser.close()
            api_stats_flush()
            sys.exit(1)

        try:
            season = detect_season(
                page,
                movie_url,
                show_name_raw
            )
        except Exception as e:
            print(
                "错误：无法可靠识别当前一级页面季数:",
                e,
                file=sys.stderr
            )
            browser.close()
            api_stats_flush()
            sys.exit(1)

        show_name = seasonize_show_name(
            show_name_raw,
            season
        )

        print(
            "一级页面主标题:",
            show_name_raw
        )
        print(
            "当前季数:",
            f"第{season}季"
        )
        print(
            "剧集名称:",
            show_name
        )

        total_episodes = detect_total_episodes(page)

        if total_episodes > 0:
            print(
                "页面真实总集数:",
                total_episodes
            )
        else:
            print(
                "页面未识别到总集数，暂不修改数据库原有值"
            )

        conn = sqlite3.connect(DB_FILE)

        try:
            conn.execute(
                "PRAGMA foreign_keys = ON"
            )

            ensure_schema(conn)

            conn.execute(
                "PRAGMA busy_timeout = 10000"
            )

            existing = conn.execute(
                """
                SELECT
                    id,
                    webdav_path,
                    total_episodes,
                    share_scan_rank,
                    discovery_cursor
                FROM shows
                WHERE seedhub_url = ?
                """,
                (
                    movie_url,
                )
            ).fetchone()

            if existing:
                show_id = existing[0]

                existing_webdav_path = (
                    existing[1] or ""
                )

                existing_total_episodes = (
                    existing[2] or 0
                )

                if existing_webdav_path:
                    webdav_path = replace_webdav_leaf(
                        existing_webdav_path,
                        show_name
                    )
                else:
                    webdav_path = (
                        "/kuake/其他/"
                        + show_name
                    )

                if total_episodes <= 0:
                    total_episodes = (
                        existing_total_episodes
                    )

                old_rank = existing[3] or 0
                discovery_cursor = existing[4] or FRONT_SHARE_LIMIT

                print(
                    "已有 show_id:",
                    show_id
                )

                print(
                    "已有 share_scan_rank:",
                    old_rank
                )
                print(
                    "已有 discovery_cursor:",
                    discovery_cursor
                )

            else:
                show_id = None
                old_rank = 0
                discovery_cursor = FRONT_SHARE_LIMIT
                webdav_path = (
                    "/kuake/其他/"
                    + show_name
                )

            if show_id is None:
                row = get_or_create_show(
                    conn,
                    show_name,
                    movie_url,
                    webdav_path,
                    total_episodes
                )
            else:
                conn.execute(
                    """
                    UPDATE shows
                    SET
                        name = ?,

                        webdav_path = ?,

                        total_episodes =
                            CASE
                                WHEN ? > 0
                                THEN ?
                                ELSE total_episodes
                            END,

                        updated_at = CURRENT_TIMESTAMP

                    WHERE id = ?
                    """,
                    (
                        show_name,
                        webdav_path,
                        total_episodes,
                        total_episodes,
                        show_id
                    )
                )

                row = conn.execute(
                    """
                    SELECT
                        id,
                        total_episodes,
                        share_scan_rank,
                        webdav_path
                    FROM shows
                    WHERE id = ?
                    """,
                    (
                        show_id,
                    )
                ).fetchone()

            show_id = row[0]
            current_total_episodes = row[1] or 0
            old_rank = row[2] or 0
            webdav_path = row[3] or webdav_path

            print(
                "show_id:",
                show_id
            )

            print(
                "WebDAV:",
                webdav_path
            )

            print(
                "数据库 total_episodes:",
                current_total_episodes
            )

            if args.range_args:
                start_rank, count = args.range_args
                rc = scan_seedhub_range(conn, page, show_id, start_rank, count)
                if rc != 0:
                    sys.exit(rc)
                return

            reconcile_front20(conn, page, show_id)
            return

        finally:
            conn.close()
            browser.close()
            api_stats_flush()


if __name__ == "__main__":
    main()