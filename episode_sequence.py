#!/usr/bin/env python3
"""Shared conservative episode-sequence inference for Quark source and WebDAV scans."""
import argparse
import os
import re
import sys

parser = argparse.ArgumentParser(description="Infer episode numbers from a scanned TSV file.")
parser.add_argument("--format", choices=("source", "webdav"), default="source")
parser.add_argument("--season", type=int, default=1)
parser.add_argument("scan_file")
args = parser.parse_args()
if not 1 <= args.season <= 99:
    parser.error("--season must be between 1 and 99")

row_format = args.format
scan_path = args.scan_file
marker = "__UNPARSED__"

def row_is_valid(row):
    return len(row) >= (7 if row_format == "source" else 4)

def row_filename(row):
    return row[4] if row_format == "source" else row[1]

def row_path(row):
    return row[6] if row_format == "source" else ""

with open(scan_path, "r", encoding="utf-8") as stream:
    rows = [line.rstrip("\n").split("\t") for line in stream if line.rstrip("\n")]

def natural_key(value):
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    )

def episode_key(value):
    match = re.fullmatch(r"S\d{2}E(\d{1,4})", value)
    return int(match.group(1)) if match else None

def candidate_tokens(filename):
    # Source and WebDAV inference must only use actual media files. A numbered
    # subtitle or artwork set must never be mistaken for missing episode videos.
    if row_format == "webdav" and filename.rsplit(".", 1)[-1].casefold() not in {
        "mkv", "mp4", "m2ts", "ts", "avi", "mov", "wmv", "flv", "webm"
    }:
        return []

    stem = filename.rsplit(".", 1)[0]
    candidates = []
    for match in re.finditer(r"(?<!\d)(\d{1,2})(?!\d)", stem):
        value = int(match.group(1))
        if value < 1:
            continue

        suffix = stem[match.end():]
        suffix_token = re.sub(r"^[\s._~-]+", "", suffix)
        # Exclude common quality/encoding tokens even when separated by punctuation:
        # 4K, 60fps, 60_fps, 24-p, 10bit, and similar metadata.
        if re.match(r"(?i)^(?:k|fps|hz|bit|p)\b", suffix_token):
            continue

        prefix = stem[:match.start()]
        labeled_before = re.search(
            r"(?i)(?:\bepisode\b|\bep\b|\bpart\b|第|集|话|回|期)\s*[-_.~#\[\]()（）【】]*$",
            prefix,
        ) is not None
        labeled_after = re.match(r"\s*(?:集|话|回|期)", suffix) is not None
        candidates.append((value, labeled_before or labeled_after))

    by_value = {}
    for value, labeled in candidates:
        by_value[value] = by_value.get(value, False) or labeled
    return [(value, labeled) for value, labeled in by_value.items()]

token_rows = {
    index: candidate_tokens((row_path(row) + "/" if row_path(row) else "") + row_filename(row))
    for index, row in enumerate(rows)
    if row_is_valid(row)
}

def state_score(path, label_votes):
    unknown_count = sum(1 for _, _, is_unknown in path if is_unknown)
    anchor_count = len(path) - unknown_count
    distinct_count = len({value for _, value, _ in path})
    # Prefer sequences that cover more files and agree with known episode formats.
    return (len(path), anchor_count, unknown_count, distinct_count, label_votes)

def offer_state(states, last_value, path, label_votes):
    candidate = (path, label_votes)
    score = state_score(path, label_votes)
    bucket = states.setdefault(last_value, [])
    if bucket:
        best_score = state_score(bucket[0][0], bucket[0][1])
        if score < best_score:
            return
        if score > best_score:
            bucket.clear()
    if any(existing_path == path for existing_path, _ in bucket):
        return
    # Preserve multiple equal-score paths so ambiguous numeric fields are rejected.
    if len(bucket) < 2:
        bucket.append(candidate)

def infer_direction(ordered, direction, minimum_coverage):
    states = {}
    for index in ordered:
        known_episode = episode_key(rows[index][0])
        if rows[index][0] != marker and known_episode is not None:
            options = [(known_episode, False, False)]
        else:
            options = [
                (value, labeled, True)
                for value, labeled in token_rows.get(index, [])
            ]

        previous_states = [
            (last_value, path, labels)
            for last_value, bucket in states.items()
            for path, labels in bucket
        ]
        next_states = {last_value: list(bucket) for last_value, bucket in states.items()}

        for value, labeled, is_unknown in options:
            offer_state(next_states, value, ((index, value, is_unknown),), int(labeled))
            for last_value, path, labels in previous_states:
                follows_order = (
                    last_value <= value if direction == "ascending"
                    else last_value >= value
                )
                if follows_order:
                    offer_state(
                        next_states,
                        value,
                        path + ((index, value, is_unknown),),
                        labels + int(labeled),
                    )
        states = next_states

    eligible = []
    for bucket in states.values():
        for path, labels in bucket:
            score = state_score(path, labels)
            if score[0] >= minimum_coverage and score[2] > 0 and score[3] >= 3:
                inferred = tuple((index, value) for index, value, is_unknown in path if is_unknown)
                eligible.append((score, inferred))

    if not eligible:
        return None

    best_score = max(score for score, _ in eligible)
    best_mappings = [
        mapping for score, mapping in eligible
        if score == best_score
    ]
    signatures = {tuple(sorted(mapping)) for mapping in best_mappings}
    if len(signatures) != 1:
        return None

    return best_score, dict(best_mappings[0])

def infer_group(indices, sort_by_path=False):
    unresolved = [index for index in indices if rows[index][0] == marker]
    if not unresolved:
        return

    ordered = sorted(
        indices,
        key=lambda index: natural_key(
            (row_path(rows[index]) + "/" if sort_by_path and row_path(rows[index]) else "")
            + row_filename(rows[index])
        ),
    )
    minimum_coverage = max(3, (len(indices) + 1) // 2)
    options = [
        infer_direction(ordered, direction, minimum_coverage)
        for direction in ("ascending", "descending")
    ]
    options = [option for option in options if option is not None]
    if not options:
        return

    best_score = max(score for score, _ in options)
    best = [mapping for score, mapping in options if score == best_score]
    signatures = {tuple(sorted(mapping.items())) for mapping in best}
    if len(signatures) != 1:
        return

    for index, value in best[0].items():
        rows[index][0] = f"S{args.season:02d}E{value:02d}"

if row_format == "source":
    directory_groups = {}
    for index, row in enumerate(rows):
        if row_is_valid(row):
            directory_groups.setdefault(row_path(row), []).append(index)

    for indices in directory_groups.values():
        infer_group(indices)

infer_group(list(range(len(rows))), sort_by_path=(row_format == "source"))

temporary_path = scan_path + ".inferred"
with open(temporary_path, "w", encoding="utf-8") as stream:
    for row in rows:
        stream.write("\t".join(row) + "\n")
os.replace(temporary_path, scan_path)
