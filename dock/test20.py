import re
import time
import builtins
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

MOVIE_URL = "https://www.seedhub.cc/movies/130168/"
BASE_URL = "https://www.seedhub.cc"
LOG_FILE = Path(__file__).resolve().parents[1] / "logs" / "test20.log"


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

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page(viewport={"width": 1920, "height": 1080})

    print("打开电影页面...")
    page.goto(
        MOVIE_URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    links = page.locator('.pan-links a[data-link*="quark"]')
    total = links.count()

    print("找到夸克入口:", total)

    # 先把前20个入口全部保存下来
    items = []

    for i in range(min(total, 20)):
        a = links.nth(i)

        href = a.get_attribute("href")
        title = a.inner_text().strip()

        if href:
            items.append({
                "title": title,
                "url": urljoin(BASE_URL, href)
            })

    print("已经保存前", len(items), "个入口")
    print("开始逐个获取 Quark 链接")

    for i, item in enumerate(items, 1):

        print("\n[%02d] %s" % (i, item["title"][:60]))
        print("     URL:", item["url"])

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

            if found:
                print("     Quark:", found[0])
            else:
                print("     没找到 Quark 链接")
                print("     HTML长度:", len(html))

        except Exception as e:
            print("     ERROR:", e)

        # 每个二级页面之间等待2秒
        if i < len(items):
            time.sleep(2)

    browser.close()
