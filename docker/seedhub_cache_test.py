import re
import sqlite3
import time
import builtins
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

MOVIE_URL = "https://www.seedhub.cc/movies/130168/"
BASE_URL = "https://www.seedhub.cc"
DB_FILE = "/data/resource.db"

SHOW_NAME = "重器"
WEBdav_PATH = "/kuake/其他/重器"

CACHE_LIMIT = 20
LOG_FILE = Path(__file__).resolve().parents[1] / "logs" / "seedhub_cache_test.log"


def log_print(*args, **kwargs):
    builtins.print(*args, **kwargs)
    message = kwargs.get("sep", " ").join(str(arg) for arg in args)
    if message:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [PARSE] {message}{kwargs.get('end', chr(10))}"
            )


print = log_print


def main():

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        page = browser.new_page(
            viewport={"width": 1920, "height": 1080}
        )

        print("打开电影页面...")

        page.goto(
            MOVIE_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        links = page.locator(
            '.pan-links a[data-link*="quark"]'
        )

        total = links.count()

        print("找到夸克入口:", total)

        items = []

        for i in range(min(total, CACHE_LIMIT)):

            a = links.nth(i)

            href = a.get_attribute("href")
            title = a.inner_text().strip()

            if href:

                items.append({
                    "rank": i + 1,
                    "title": title,
                    "url": urljoin(BASE_URL, href)
                })

        print("准备缓存:", len(items), "条")

        conn = sqlite3.connect(DB_FILE)

        conn.execute("PRAGMA foreign_keys = ON")

        # 创建/获取剧集
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
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(seedhub_url)
            DO UPDATE SET
                name = excluded.name,
                webdav_path = excluded.webdav_path,
                total_episodes = excluded.total_episodes,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                SHOW_NAME,
                MOVIE_URL,
                WEBdav_PATH,
                40,
                0,
                20
            )
        )

        show_id = conn.execute(
            "SELECT id FROM shows WHERE seedhub_url = ?",
            (MOVIE_URL,)
        ).fetchone()[0]

        print("show_id:", show_id)

        for item in items:

            print(
                "[%02d] 获取 Quark..."
                % item["rank"]
            )

            try:

                page.goto(
                    item["url"],
                    wait_until="domcontentloaded",
                    timeout=60000
                )

                html = page.content()

                found = re.findall(
                    r'https?://pan\.quark\.cn/[^\s"\'<>]+',
                    html
                )

                found = list(dict.fromkeys(found))

                if not found:

                    print("     没找到 Quark")
                    continue

                quark_url = found[0]

                print(
                    "     ",
                    quark_url
                )

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
                        item["rank"]
                    )
                )

            except Exception as e:

                print(
                    "     ERROR:",
                    e
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
                len(items),
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
            (show_id,)
        ).fetchone()[0]

        conn.close()

        print()
        print("数据库写入完成")
        print("show_id:", show_id)
        print("缓存分享数量:", count)
        print("share_scan_rank:", len(items))

        browser.close()


if __name__ == "__main__":
    main()
