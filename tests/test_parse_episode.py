#!/usr/bin/env python3
"""Regression tests for shared episode parsing and sequence inference."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_CHECK = (ROOT / "source_check.sh").read_text(encoding="utf-8")
ADDFILE = (ROOT / "addfile.sh").read_text(encoding="utf-8")
EPISODE_SEQUENCE = ROOT / "episode_sequence.py"


def extract_bash_function(source, name):
    start = source.find(f"{name}() {{\n")
    if start < 0:
        raise AssertionError(f"Could not find Bash function: {name}")
    pattern = re.compile(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}$")
    match = pattern.search(source, start)
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
            "02~4K.mp4": "S01E02",
            "13-4K.高码率.mp4": "S01E13",
            "14-1080P.高码率.mp4": "S01E14",
            "07_2160p_HDR.mkv": "S01E07",
            "08 1080p Web-DL.mkv": "S01E08",
            "09[1080p].mkv": "S01E09",
            "10(4K).mkv": "S01E10",
            "11【1080p】.mp4": "S01E11",
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


    def test_number_led_episode_candidates_reject_three_digit_numbers(self):
        for filename in ("123 - Complete.Title.mkv", "1000-2160p.Movie.mkv"):
            with self.subTest(filename=filename):
                result = self.parse(filename)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def infer_rows(self, rows, *, format="source", season=1):
        with tempfile.TemporaryDirectory() as tempdir:
            scan_file = Path(tempdir) / "scan.tsv"
            scan_file.write_text(
                "".join("\t".join(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            command = [
                "python3", str(EPISODE_SEQUENCE),
                "--season", str(season),
            ]
            if format != "source":
                command.extend(["--format", format])
            command.append(str(scan_file))
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return [line.split("\t") for line in scan_file.read_text(encoding="utf-8").splitlines()]

    def test_infers_episode_numbers_anywhere_in_incrementing_file_set(self):
        numbers = (2, 6, 8, 22, 24)
        rows = [
            ["__UNPARSED__", "200000000", str(i), "token", f"Show source {n}~4K SDR 60fps.mp4", "mp4", ""]
            for i, n in enumerate(numbers)
        ]
        result = self.infer_rows(rows)
        self.assertEqual([row[0] for row in result], [f"S01E{n:02d}" for n in numbers])

    def test_infers_descending_episode_sequence_when_numeric_field_is_not_first(self):
        samples = [
            ("Alpha release 24~4K.mp4", 24),
            ("Bravo release 22~4K.mp4", 22),
            ("Charlie release 08~4K.mp4", 8),
            ("Delta release 06~4K.mp4", 6),
            ("Echo release 02~4K.mp4", 2),
        ]
        rows = [
            ["__UNPARSED__", "200000000", str(i), "token", filename, "mp4", ""]
            for i, (filename, _) in enumerate(samples)
        ]
        result = self.infer_rows(rows)
        self.assertEqual(
            [row[0] for row in result],
            [f"S01E{number:02d}" for _, number in samples],
        )

    def test_inference_ignores_resolution_year_and_frame_rate_numbers(self):
        numbers = (1, 2, 3, 4)
        rows = [
            ["__UNPARSED__", "200000000", str(i), "token",
             f"Series version {n} 2024 1080p 4K 60fps.mkv", "mkv", ""]
            for i, n in enumerate(numbers)
        ]
        result = self.infer_rows(rows)
        self.assertEqual([row[0] for row in result], [f"S01E{n:02d}" for n in numbers])


    def test_infers_episode_number_from_numeric_parent_directory(self):
        numbers = (2, 6, 8, 22, 24)
        rows = [
            ["__UNPARSED__", "200000000", str(i), "token", "EpisodeClip.mp4", "mp4", f"Disc {n:02d}/"]
            for i, n in enumerate(numbers)
        ]
        result = self.infer_rows(rows)
        self.assertEqual([row[0] for row in result], [f"S01E{n:02d}" for n in numbers])


    def test_infers_when_episode_number_moves_between_numeric_positions(self):
        samples = [
            ("Series 02~4K.mp4", 2),
            ("Series 03 filler Episode 06~4K.mp4", 6),
            ("Series 08~4K.mp4", 8),
            ("Series 22~4K.mp4", 22),
            ("Series 24~4K.mp4", 24),
        ]
        rows = [
            ["__UNPARSED__", "200000000", str(i), "token", filename, "mp4", ""]
            for i, (filename, _) in enumerate(samples)
        ]
        result = self.infer_rows(rows)
        self.assertEqual(
            [row[0] for row in result],
            [f"S01E{number:02d}" for _, number in samples],
        )


    def test_existing_parsed_episode_anchors_help_infer_unparsed_files(self):
        samples = [
            ("S01E02", "02.4K.SDR.60fps.mp4"),
            ("__UNPARSED__", "Series 06~4K.mp4"),
            ("__UNPARSED__", "Series 08~4K.mp4"),
            ("__UNPARSED__", "Series 22~4K.mp4"),
            ("__UNPARSED__", "Series 24~4K.mp4"),
        ]
        rows = [
            [episode, "200000000", str(i), "token", filename, "mp4", ""]
            for i, (episode, filename) in enumerate(samples)
        ]
        result = self.infer_rows(rows)
        self.assertEqual(
            [row[0] for row in result],
            ["S01E02", "S01E06", "S01E08", "S01E22", "S01E24"],
        )


    def test_single_unparsed_file_can_be_inferred_from_a_broad_anchor_sequence(self):
        samples = [
            ("S01E01", "EP01.mp4"),
            ("S01E02", "EP02.mp4"),
            ("S01E03", "EP03.mp4"),
            ("S01E04", "EP04.mp4"),
            ("__UNPARSED__", "Episode source 06~4K.mp4"),
        ]
        rows = [
            [episode, "200000000", str(i), "token", filename, "mp4", ""]
            for i, (episode, filename) in enumerate(samples)
        ]
        result = self.infer_rows(rows)
        self.assertEqual(
            [row[0] for row in result],
            ["S01E01", "S01E02", "S01E03", "S01E04", "S01E06"],
        )


    def test_ignores_fps_numbers_with_separators_when_inferring_episodes(self):
        samples = ((2, 24), (6, 30), (8, 48), (22, 60))
        rows = [
            ["__UNPARSED__", "200000000", str(i), "token",
             f"Series source {episode}~4K {fps}_fps.mp4", "mp4", ""]
            for i, (episode, fps) in enumerate(samples)
        ]
        result = self.infer_rows(rows)
        self.assertEqual(
            [row[0] for row in result],
            [f"S01E{episode:02d}" for episode, _ in samples],
        )

    def test_keeps_ambiguous_numeric_fields_unresolved(self):
        numbers = ((2, 11), (6, 14), (8, 16))
        rows = [
            ["__UNPARSED__", "200000000", str(i), "token",
             f"Series {first:02d} cut {second:02d}.mp4", "mp4", ""]
            for i, (first, second) in enumerate(numbers)
        ]
        result = self.infer_rows(rows)
        self.assertEqual([row[0] for row in result], ["__UNPARSED__"] * len(numbers))

    def test_does_not_infer_without_a_consistent_multi_file_sequence(self):
        filenames = ["Movie release 2024 4K.mp4", "Movie release 2025 4K.mp4"]
        rows = [
            ["__UNPARSED__", "200000000", str(i), "token", filename, "mp4", ""]
            for i, filename in enumerate(filenames)
        ]
        result = self.infer_rows(rows)
        self.assertEqual([row[0] for row in result], ["__UNPARSED__", "__UNPARSED__"])

    def test_inference_supports_webdav_rows_and_requested_season(self):
        numbers = (2, 6, 8, 22)
        rows = [
            ["__UNPARSED__", f"Series source {number}~4K.mkv", "200000000", ""]
            for number in numbers
        ]
        result = self.infer_rows(rows, format="webdav", season=2)
        self.assertEqual(
            [row[0] for row in result],
            [f"S02E{number:02d}" for number in numbers],
        )

    def test_inference_does_not_use_non_video_webdav_files_as_candidates(self):
        numbers = (2, 6, 8, 22)
        rows = [
            ["__UNPARSED__", f"Series source {number}~4K.srt", "200000000", ""]
            for number in numbers
        ]
        result = self.infer_rows(rows, format="webdav")
        self.assertEqual([row[0] for row in result], ["__UNPARSED__"] * len(numbers))

    def test_target_matching_falls_back_to_exact_source_filename(self):
        function = extract_bash_function(ADDFILE, "target_file_matches_candidate")
        cases = [
            # Filename itself is not parseable as an episode, but it is the
            # exact source filename and its size is checked by the caller.
            ("Show release 15~4K.mp4", "", "S01E15", "Show release 15~4K.mp4", 0),
            # Existing explicit parsing remains preferred and supported.
            ("S01E15.mp4", "S01E15", "S01E15", "original-name.mp4", 0),
            # Neither the expected episode nor exact source filename matches.
            ("unrelated.mp4", "", "S01E15", "Show release 15~4K.mp4", 1),
        ]
        for target_name, target_episode, expected_episode, source_name, expected_rc in cases:
            with self.subTest(target_name=target_name, expected_rc=expected_rc):
                result = subprocess.run(
                    [
                        "bash", "-c",
                        function + '\ntarget_file_matches_candidate "$1" "$2" "$3" "$4"\n',
                        "target-file-match-test",
                        target_name, target_episode, expected_episode, source_name,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_rc, result.stderr)

    def test_missing_target_file_is_not_reported_as_success(self):
        start = ADDFILE.index('warn "转存成功但目标目录暂时找不到')
        end = ADDFILE.index('\n        fi', start)
        failure_block = ADDFILE[start:end]
        self.assertIn("task_failed=1", failure_block)

    def test_all_scanning_scripts_use_the_shared_sequence_inference(self):
        source_check = (ROOT / "source_check.sh").read_text(encoding="utf-8")
        addfile = (ROOT / "addfile.sh").read_text(encoding="utf-8")
        resource_check = (ROOT / "resource_check.sh").read_text(encoding="utf-8")

        self.assertIn('python3 "$EPISODE_SEQUENCE_PY" --season 1 "$scan_file"', source_check)
        self.assertIn('[ -n "$key" ] || key="__UNPARSED__"', addfile)
        self.assertIn('python3 "$EPISODE_SEQUENCE_PY" --season "$TASK_SEASON" "$existing_scan"', addfile)
        self.assertIn('python3 "$EPISODE_SEQUENCE_PY" --season "$TASK_SEASON" "$share_file"', addfile)
        self.assertIn('python3 "$EPISODE_SEQUENCE_PY" --format webdav --season "$season" "$output"', resource_check)


if __name__ == "__main__":
    unittest.main()
