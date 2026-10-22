#!/usr/bin/env python3
"""Regression contracts for forced Quark-share refresh and serialized web jobs."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RESOURCE_CHECK = (ROOT / "resource_check.sh").read_text(encoding="utf-8")
WEB_APP = (ROOT / "web_app.py").read_text(encoding="utf-8")


class ForcedRefreshContractTests(unittest.TestCase):
    def test_resource_check_refreshes_every_non_excluded_share_without_caps(self):
        start = RESOURCE_CHECK.index("scan_all_shares() {")
        end = RESOURCE_CHECK.index("\nrecheck_pending_shares() {", start)
        refresh_function = RESOURCE_CHECK[start:end]

        self.assertIn("FROM shares", refresh_function)
        self.assertIn("show_id=", refresh_function)
        self.assertIn("status,'unknown')!='excluded'", refresh_function)
        self.assertNotIn("last_check", refresh_function)
        self.assertNotIn("LIMIT ", refresh_function)
        self.assertNotIn("status='valid'", refresh_function)

    def test_each_resource_forces_share_refresh_before_webdav_cache_is_read(self):
        process_start = RESOURCE_CHECK.index("process_resource() {")
        process_end = RESOURCE_CHECK.index("\n# ============================================================\n# 读取 resources.json", process_start)
        process_body = RESOURCE_CHECK[process_start:process_end]

        refresh_at = process_body.index('scan_all_shares "$show_id" "$resource_name"')
        webdav_at = process_body.index('scan_webdav_actual "$target" "$webdav_file"')
        self.assertLess(refresh_at, webdav_at)

    def test_web_task_requests_are_persistently_queued_instead_of_rejected_when_busy(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS web_task_queue", WEB_APP)
        self.assertIn("INSERT INTO web_task_queue", WEB_APP)
        self.assertIn("def task_queue_worker(", WEB_APP)
        self.assertIn("def start_task(", WEB_APP)
        task_start = WEB_APP.index("def start_task(")
        task_end = WEB_APP.index("\n@app.route('/api-stats')", task_start)
        start_task_body = WEB_APP[task_start:task_end]
        self.assertNotIn("后台任务启动被拒绝", start_task_body)


if __name__ == "__main__":
    unittest.main()
