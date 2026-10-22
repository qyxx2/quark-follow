#!/usr/bin/env python3
"""Regression tests for source_check.sh episode-number parsing."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_CHECK = (ROOT / "source_check.sh").read_text(encoding="utf-8")


def extract_bash_function(source, name):
    pattern = re.compile(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}$")
    match = pattern.search(source)
    if not match:
        raise AssertionError(f"Could not find Bash function: {name}")
    return match.group(0)


class ParseEpisodeRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = "\n\n".join(
            extract_bash_function(SOURCE_CHECK, name)
            for name in ("cn_number", "normalize_circled", "parse_episode")
        )

    def parse(self, filename):
        return subprocess.run(
            [
                "bash",
                "-c",
                self.functions + '\nparse_episode "$1"\n',
                "parse-episode-test",
                filename,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_leading_numeric_episode_with_quality_suffixes(self):
        cases = {
            "01.4K.SDR.60fps..mp4": "S01E01",
            "02.4K.SDR.60fps.mp4": "S01E02",
            "13-4K.高码率.mp4": "S01E13",
            "14-1080P.高码率.mp4": "S01E14",
            "07_2160p_HDR.mkv": "S01E07",
            "08 1080p Web-DL.mkv": "S01E08",
            "09[1080p].mkv": "S01E09",
            "10(4K).mkv": "S01E10",
            "11【1080p】.mp4": "S01E11",
            "123 - Complete.Title.mkv": "S01E123",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                result = self.parse(filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_existing_episode_formats_remain_supported(self):
        cases = {
            "01.mp4": "S01E01",
            "S02E13.mkv": "S02E13",
            "EP12.mp4": "S01E12",
            "Episode 14.mkv": "S01E14",
            "第十二集.mp4": "S01E12",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                result = self.parse(filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_year_and_resolution_prefixes_are_not_mistaken_for_episodes(self):
        for filename in ("2024.4K.Movie.mp4", "1080p.Movie.mp4", "1000-2160p.Movie.mkv"):
            with self.subTest(filename=filename):
                result = self.parse(filename)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
