#!/usr/bin/env python3

import re
import sys
import time
import sqlite3
from datetime import datetime
from pathlib import Path
import builtins
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright


# ============================================================
# 基本配置
# ============================================================

BASE_URL = "https://www.seedhub.cc"
DB_FILE = "/data/resource.db"
LOG_FILE = Path("/data/logs/seedhub_cache.log")

SCAN_BATCH_SIZE = 20
PAGE_TIMEOUT = 60000
PAGE_DELAY = 2

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
    纯英文标题则去掉英文 Season/SN 标记后补“第N季”。
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
    rank
):
    conn.execute(
        """
        INSERT INTO shares
        (
            show_id,
            url,
            seedhub_rank,
            status,
            fail_count
        )
        VALUES (?, ?, ?, 'unknown', 0)

        ON CONFLICT(show_id, url)
        DO UPDATE SET

            seedhub_rank = excluded.seedhub_rank,

            updated_at = CURRENT_TIMESTAMP
        """,
        (
            show_id,
            quark_url,
            rank
        )
    )


# ============================================================
# 计算新的 share_scan_rank
# ============================================================

def calculate_new_scan_rank(
    old_rank,
    attempted_items,
    successful_ranks
):
    new_rank = old_rank

    for item in attempted_items:
        rank = item["rank"]

        if rank != new_rank + 1:
            break

        if rank not in successful_ranks:
            break

        new_rank = rank

    return new_rank


# ============================================================
# 主程序
# ============================================================

def main():
    if len(sys.argv) != 2:
        print(
            "用法: python3 seedhub_cache.py <SeedHub一级页面URL>",
            file=sys.stderr
        )
        sys.exit(2)

    movie_url = sys.argv[1]

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
            print(
                "一级页面打开失败:",
                e,
                file=sys.stderr
            )
            browser.close()
            sys.exit(1)

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

            conn.execute(
                "PRAGMA busy_timeout = 10000"
            )

            existing = conn.execute(
                """
                SELECT
                    id,
                    webdav_path,
                    total_episodes,
                    share_scan_rank
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

                print(
                    "已有 show_id:",
                    show_id
                )

                print(
                    "已有 share_scan_rank:",
                    old_rank
                )

            else:
                show_id = None
                old_rank = 0
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

            start_rank = old_rank + 1

            items, total_links = get_quark_entries(
                page,
                start_rank,
                SCAN_BATCH_SIZE
            )

            print(
                "当前数据库已扫描到:",
                old_rank
            )

            print(
                "本次从 rank",
                start_rank,
                "开始"
            )

            print(
                "本次最多扫描:",
                SCAN_BATCH_SIZE
            )

            print(
                "本次实际准备扫描:",
                len(items)
            )

            if not items:
                print()
                print(
                    "没有新的 SeedHub 分享需要缓存。"
                )

                conn.execute(
                    """
                    UPDATE shows
                    SET
                        last_scan = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        show_id,
                    )
                )

                conn.commit()

                count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM shares
                    WHERE show_id = ?
                    """,
                    (
                        show_id,
                    )
                ).fetchone()[0]

                print()
                print("==============================")
                print("SeedHub 缓存完成")
                print("==============================")
                print("show_id:", show_id)
                print("名称:", show_name)
                print("当前季数:", f"第{season}季")
                print(
                    "一级页面 Quark 入口:",
                    total_links
                )
                print(
                    "数据库分享总数:",
                    count
                )
                print(
                    "share_scan_rank:",
                    old_rank
                )
                print(
                    "WebDAV:",
                    webdav_path
                )
                print("==============================")

                return

            success = 0
            failed = 0
            successful_ranks = set()

            print()
            print(
                "开始逐个获取 Quark 链接"
            )

            for index, item in enumerate(items):
                rank = item["rank"]

                print()
                print(
                    "[%02d] %s"
                    % (
                        rank,
                        item["title"][:100]
                    )
                )

                print(
                    "     二级:",
                    item["url"]
                )

                try:
                    page.goto(
                        item["url"],
                        wait_until="domcontentloaded",
                        timeout=PAGE_TIMEOUT
                    )

                    html = page.content()

                    quark_url = extract_quark_url(
                        html
                    )

                    if not quark_url:
                        print(
                            "     没找到 Quark"
                        )
                        failed += 1
                        continue

                    print(
                        "     Quark:",
                        quark_url
                    )

                    save_share(
                        conn,
                        show_id,
                        quark_url,
                        rank
                    )

                    successful_ranks.add(
                        rank
                    )

                    success += 1

                except Exception as e:
                    print(
                        "     ERROR:",
                        e
                    )
                    failed += 1

                if index < len(items) - 1:
                    time.sleep(
                        PAGE_DELAY
                    )

            new_rank = calculate_new_scan_rank(
                old_rank,
                items,
                successful_ranks
            )

            conn.execute(
                """
                UPDATE shows
                SET
                    share_scan_rank = ?,
                    last_scan = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    new_rank,
                    show_id
                )
            )

            conn.commit()

            count = conn.execute(
                """
                SELECT COUNT(*)
                FROM shares
                WHERE show_id = ?
                """,
                (
                    show_id,
                )
            ).fetchone()[0]

            print()
            print(
                "=============================="
            )
            print("SeedHub 缓存完成")
            print("==============================")
            print("show_id:", show_id)
            print("名称:", show_name)
            print("当前季数:", f"第{season}季")
            print(
                "一级页面 Quark 入口:",
                total_links
            )
            print(
                "本次扫描:",
                len(items)
            )
            print(
                "成功获取:",
                success
            )
            print(
                "失败:",
                failed
            )
            print(
                "数据库分享总数:",
                count
            )
            print(
                "旧 share_scan_rank:",
                old_rank
            )
            print(
                "新 share_scan_rank:",
                new_rank
            )
            print(
                "total_episodes:",
                current_total_episodes
            )
            print(
                "WebDAV:",
                webdav_path
            )
            print("==============================")

            if success == 0:
                sys.exit(1)

        finally:
            conn.close()
            browser.close()


if __name__ == "__main__":
    main()
