#!/usr/bin/env python3
"""Regression contracts for forced Quark-share refresh and serialized web jobs."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RESOURCE_CHECK = (ROOT / "resource_check.sh").read_text(encoding="utf-8")
SOURCE_CHECK = (ROOT / "source_check.sh").read_text(encoding="utf-8")
WEB_APP = (ROOT / "web_app.py").read_text(encoding="utf-8")


class ForcedRefreshContractTests(unittest.TestCase):
    def test_resource_check_refreshes_all_shares_before_reading_webdav_state(self):
        process_start = RESOURCE_CHECK.index("process_resource() {")
        process_end = RESOURCE_CHECK.index(
            "\n# ============================================================\n# 读取 resources.json",
            process_start,
        )
        process_body = RESOURCE_CHECK[process_start:process_end]

        refresh_at = process_body.index(
            'force_refresh_shares "$resource_name" "$show_id"'
        )
        webdav_at = process_body.index(
            'scan_webdav_actual "$target" "$webdav_file"'
        )
        helper_start = RESOURCE_CHECK.index("force_refresh_shares() {")
        helper_end = RESOURCE_CHECK.index("\nrecheck_pending_shares() {", helper_start)
        helper = RESOURCE_CHECK[helper_start:helper_end]

        self.assertLess(refresh_at, webdav_at)
        self.assertIn('bash "$SOURCE_CHECK_SH" --show-id "$show_id" --force', helper)

    def test_force_mode_includes_dead_shares_but_preserves_explicit_exclusions(self):
        self.assertIn("FORCE_REFRESH=false", SOURCE_CHECK)
        self.assertIn("SOURCE强制刷新失败", SOURCE_CHECK)
        self.assertIn("status='pending'", SOURCE_CHECK)
        self.assertIn("FORCE_REFRESH=true", SOURCE_CHECK)
        self.assertIn("--force)", SOURCE_CHECK)
        self.assertIn("COALESCE(status,'unknown')!='excluded'", SOURCE_CHECK)
        self.assertIn('WHERE $status_filter ORDER BY show_id, seedhub_rank, id', SOURCE_CHECK)
        self.assertIn('WHERE show_id=$(sql_quote "$TARGET_ID") AND $status_filter', SOURCE_CHECK)

    def test_share_buttons_and_resource_buttons_submit_serialized_jobs(self):
        self.assertIn("def task_queue_worker(", WEB_APP)
        self.assertIn("CREATE TABLE IF NOT EXISTS web_task_queue", WEB_APP)
        self.assertIn("INSERT INTO web_task_queue", WEB_APP)
        self.assertIn("background_work_running(task_status())", WEB_APP)
        self.assertNotIn("后台任务启动被拒绝", WEB_APP)
        self.assertIn("return start_task('resource_check.sh', [], 'resource_check')", WEB_APP)
        self.assertIn("return start_task('resource_check.sh', ['--show-id', str(show_id)], '重新检查资源')", WEB_APP)
        self.assertIn("return start_task('source_check.sh', [f'--{kind}-id', str(target_id)], '数据库同步')", WEB_APP)


    def test_global_header_exposes_openlist_from_config_and_hides_empty_value(self):
        template_start = WEB_APP.index("BASE_TEMPLATE =")
        template_end = WEB_APP.index("\n\n@app.route('/login'", template_start)
        template = WEB_APP[template_start:template_end]

        self.assertIn("@app.context_processor", WEB_APP)
        self.assertIn("def inject_openlist_url():", WEB_APP)
        self.assertIn('config_values().get("OPENLIST_URL", "").strip()', WEB_APP)
        self.assertIn("{% if openlist_url %}", template)
        self.assertIn("href='{{ openlist_url }}'", template)
        self.assertIn("target='_blank' rel='noopener noreferrer'", template)


if __name__ == "__main__":
    unittest.main()
