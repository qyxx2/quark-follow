#!/usr/bin/env python3

import re
import sys
import time
import sqlite3
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright


# ============================================================
# 基本配置
# ============================================================

BASE_URL = "https://www.seedhub.cc"
DB_FILE = "/data/resource.db"

# 每次最多新增扫描多少个 SeedHub 分享
SCAN_BATCH_SIZE = 20

# Playwright 页面超时时间
PAGE_TIMEOUT = 60000

# 两个二级页面之间的间隔
PAGE_DELAY = 2


# ============================================================
# 文本处理
# ============================================================

def normalize_text(text):
    """
    清理 SeedHub 页面提取出来的文本。

    SeedHub 的中文文本通过 inner_text() 获取后，
    经常会出现：

        云 雀 叫 天 录

    这种字符之间带空格的情况。

    对连续中文字符之间的空格进行清理。
    """

    if not text:
        return ""

    text = text.replace("\r", "\n")

    # 去掉中文字符之间的空白
    text = re.sub(
        r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])',
        '',
        text
    )

    # 多个普通空白压缩成一个
    text = re.sub(r'[ \t]+', ' ', text)

    # 清理每一行两端
    lines = []

    for line in text.splitlines():
        line = line.strip()

        if line:
            lines.append(line)

    return "\n".join(lines)


# ============================================================
# 获取剧名
# ============================================================

def get_show_name(page):
    """
    从一级 SeedHub 页面获取剧名。

    当前页面实际结构中，h1 可能得到：

        #
        云 雀 叫 天 录

    所以：
        1. 清理中文字符之间的空格
        2. 忽略单独的 #
        3. 取第一个有效标题
    """

    # --------------------------------------------------------
    # 优先尝试 h1
    # --------------------------------------------------------

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

                # 页面中的独立 # 是装饰，不是剧名
                if line == "#":
                    continue

                # 去掉开头的 #
                line = re.sub(
                    r'^#\s*',
                    '',
                    line
                ).strip()

                if line:
                    lines.append(line)

            if lines:
                return lines[0]

    except Exception:
        pass

    # --------------------------------------------------------
    # 如果 h1 没有得到结果，再尝试 title
    # --------------------------------------------------------

    try:
        title = normalize_text(page.title())

        if title:

            # 去掉常见站点后缀
            title = re.sub(
                r'\s*[-|｜]\s*SeedHub.*$',
                '',
                title,
                flags=re.I
            )

            title = re.sub(
                r'^#\s*',
                '',
                title
            ).strip()

            if title:
                return title

    except Exception:
        pass

    return ""


# ============================================================
# 获取真实总集数
# ============================================================

def detect_total_episodes(page):
    """
    从 SeedHub 一级页面的影片信息中读取真实集数。

    当前实际页面文本：

        集 数 : 40

    因此只针对“集数”字段进行解析。

    注意：
        这里的 total_episodes 是 SeedHub 一级页面
        提供的剧集信息。

        绝不能使用：
            Quark 分享最高集数
            资源标题中的“更新至XX集”
            share_files 最高 episode

        来代替这个值。
    """

    try:

        body_text = page.locator("body").inner_text()

        # 先清理中文字符之间的空格
        body_text = normalize_text(body_text)

        patterns = [

            # 集 数 : 40
            r'集\s*数\s*[:：]\s*(\d+)',

            # 集数：40
            r'集数\s*[:：]\s*(\d+)',

        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                body_text,
                re.I
            )

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
    """
    获取指定 rank 范围内的 Quark 入口。

    start_rank:
        从哪个 SeedHub rank 开始。

    max_count:
        本次最多获取多少个。

    例如：

        share_scan_rank = 12
        页面当前有 13 个

    那么：

        start_rank = 13

    只获取第13个。
    """

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

        # Playwright nth() 从 0 开始
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
    """
    从 SeedHub 二级页面 HTML 中提取最终：

        https://pan.quark.cn/s/xxxxx
    """

    found = re.findall(
        r'https?://pan\.quark\.cn/[^\s"\'<>]+',
        html
    )

    # 去重，同时保持原顺序
    found = list(dict.fromkeys(found))

    if not found:
        return ""

    return found[0]


# ============================================================
# 数据库：获取 / 创建 show
# ============================================================

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
    """
    只有连续成功的 rank 才推进 share_scan_rank。

    例如：

        old_rank = 12

        rank 13 成功
        rank 14 成功
        rank 15 失败
        rank 16 成功

    那么最终：

        share_scan_rank = 14

    而不是 16。

    这样 rank 15 下次还能继续尝试。
    """

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

    # --------------------------------------------------------
    # URL 基本检查
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Playwright
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # 打开一级页面
        # ----------------------------------------------------

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
        # 解析剧名
        # ----------------------------------------------------

        show_name = get_show_name(page)

        if not show_name:

            print(
                "错误：无法从一级页面获取剧集名称",
                file=sys.stderr
            )

            browser.close()

            sys.exit(1)

        print(
            "剧集名称:",
            show_name
        )

        # ----------------------------------------------------
        # 解析真实总集数
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # 打开数据库
        # ----------------------------------------------------

        conn = sqlite3.connect(DB_FILE)

        try:

            conn.execute(
                "PRAGMA foreign_keys = ON"
            )

            conn.execute(
                "PRAGMA busy_timeout = 10000"
            )

            # ------------------------------------------------
            # 查找已有 show
            # ------------------------------------------------

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

                old_rank = (
                    existing[3] or 0
                )

                # 已经有 WebDAV 路径就保留
                if existing_webdav_path:

                    webdav_path = (
                        existing_webdav_path
                    )

                else:

                    webdav_path = (
                        "/kuake/其他/"
                        + show_name
                    )

                # 页面没识别到总集数时，
                # 保留数据库原来的值
                if total_episodes <= 0:

                    total_episodes = (
                        existing_total_episodes
                    )

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

            # ------------------------------------------------
            # 创建 / 更新 show
            # ------------------------------------------------

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

            # ------------------------------------------------
            # 获取需要新增扫描的 Quark 入口
            # ------------------------------------------------

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

            # ------------------------------------------------
            # 没有新的 SeedHub 分享
            # ------------------------------------------------

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

            # ------------------------------------------------
            # 开始逐个打开二级页面
            # ------------------------------------------------

            success = 0
            failed = 0

            successful_ranks = set()

            print()
            print(
                "开始逐个获取 Quark 链接"
            )

            for index, item in enumerate(
                items
            ):

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

                # 二级页面之间等待
                if index < len(items) - 1:

                    time.sleep(
                        PAGE_DELAY
                    )

            # ------------------------------------------------
            # 计算新的扫描进度
            # ------------------------------------------------

            new_rank = calculate_new_scan_rank(
                old_rank,
                items,
                successful_ranks
            )

            # ------------------------------------------------
            # 更新 shows
            # ------------------------------------------------

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

            # ------------------------------------------------
            # 查询最终分享数量
            # ------------------------------------------------

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

            # ------------------------------------------------
            # 输出结果
            # ------------------------------------------------

            print()
            print(
                "=============================="
            )

            print(
                "SeedHub 缓存完成"
            )

            print(
                "=============================="
            )

            print(
                "show_id:",
                show_id
            )

            print(
                "名称:",
                show_name
            )

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

            print(
                "=============================="
            )

            # 如果本次一个都没成功，
            # 返回失败给上层调用者
            if success == 0:

                sys.exit(1)

        finally:

            conn.close()
            browser.close()


if __name__ == "__main__":
    main()
