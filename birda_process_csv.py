#!/usr/bin/env python3

## @file birda_process_csv.py
# @brief Merge detection CSVs, report species totals, and deduplicate 15-second blocks.
# @details Accepts a CSV path or directory tree. Writes birda_merged.csv and
# birda_merged_15s_dedup.csv in the working directory. Keeps the first row for
# each source file, species, and block without changing detection offsets.
# Input files are read in deterministic directory and filename order. The first
# output preserves the union of all input columns; the deduplicated output keeps
# the first usable row for each (File, species, 15-second block) key. Rows that
# cannot form a complete key pass through unchanged. Species statistics describe
# the merged input before deduplication.
# @par Command line
# @code{.sh}
# python3 birda_process_csv.py /path/to/per-recording-csvs
# @endcode
# @see birda_csv_to_birdnet.py

import csv
import math
import os
import sys
from collections import Counter


OUTPUT_FILENAME = "birda_merged.csv"
DEDUPED_OUTPUT_FILENAME = "birda_merged_15s_dedup.csv"
BLOCK_SECONDS = 15
SPECIES_FIELDS = (
    "Scientific name",
    "scientific name",
    "Species",
    "species",
    "Common name",
    "common name",
)
COMMON_NAME_FIELDS = (
    "Common name",
    "common name",
)
CONFIDENCE_FIELDS = (
    "Confidence",
    "confidence",
    "Score",
    "score",
)
START_FIELDS = (
    "Start (s)",
    "start (s)",
)


## @brief Report a CSV processing failure to stderr and terminate.
# @param message Human-readable failure description.
# @exception SystemExit Always raised with exit status 1.
def error(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


## @brief Discover CSV inputs in deterministic directory and filename order.
# @param root_path CSV file or directory to search recursively.
# @return List of input paths excluding this run's two output paths.
# @exception SystemExit Raised for an invalid path or an explicit generated output.
# @note Other directories' generated CSVs and unrelated CSV files are not excluded.
def find_csv_files(root_path):
    output_paths = {
        os.path.abspath(os.path.join(os.getcwd(), OUTPUT_FILENAME)),
        os.path.abspath(os.path.join(os.getcwd(), DEDUPED_OUTPUT_FILENAME)),
    }

    if os.path.isfile(root_path):
        if not root_path.lower().endswith(".csv"):
            error(f"Path is not a CSV file: {root_path}")

        if os.path.abspath(root_path) in output_paths:
            error(f"Path is a generated output file: {root_path}")

        return [root_path]

    if not os.path.isdir(root_path):
        error(f"Path does not exist: {root_path}")

    csv_files = []

    for current_dir, dirs, files in os.walk(root_path):
        dirs.sort()

        for filename in sorted(files):
            if filename.lower().endswith(".csv"):
                csv_files.append(
                    os.path.abspath(
                        os.path.join(current_dir, filename)
                    )
                )

    return [
        path
        for path in csv_files
        if os.path.abspath(path) not in output_paths
    ]


## @brief Open a UTF-8 CSV for reading, accepting an optional byte-order mark.
# @param csv_file Input CSV path.
# @return Open text handle; the caller must close it.
# @exception SystemExit Raised when the file cannot be opened.
def open_reader(csv_file):
    try:
        return open(
            csv_file,
            "r",
            newline="",
            encoding="utf-8-sig"
        )
    except OSError as exc:
        error(f"Cannot open CSV file {csv_file}: {exc}")


## @brief Collect the union of input headers while preserving first-seen order.
# @param csv_files Ordered input CSV paths.
# @return Ordered list of distinct column names.
# @exception SystemExit Raised for inaccessible files or missing headers.
def collect_fieldnames(csv_files):
    fieldnames = []
    seen_fields = set()

    for csv_file in csv_files:
        with open_reader(csv_file) as input_handle:
            reader = csv.DictReader(input_handle)

            if reader.fieldnames is None:
                error(f"No CSV header in {csv_file}")

            for field in reader.fieldnames:
                if field not in seen_fields:
                    fieldnames.append(field)
                    seen_fields.add(field)

    return fieldnames


## @brief Resolve a CSV value from an ordered list of column aliases.
# @param row Dictionary produced by csv.DictReader.
# @param field_candidates Column names in preference order.
# @return First nonempty stripped value, or an empty string.
def get_first_non_empty_value(row, field_candidates):
    for field in field_candidates:
        value = row.get(field)

        if value is not None:
            value = value.strip()

            if value:
                return value

    return ""


## @brief Resolve species identity, preferring scientific-name aliases.
# @param row Detection CSV dictionary.
# @return Stripped species value, or an empty string when absent.
def get_species_value(row):
    return get_first_non_empty_value(row, SPECIES_FIELDS)


## @brief Resolve the display name from supported common-name columns.
# @param row Detection CSV dictionary.
# @return Stripped common name, or an empty string when absent.
def get_common_name_value(row):
    return get_first_non_empty_value(row, COMMON_NAME_FIELDS)


## @brief Parse a confidence value from supported score columns.
# @param row Detection CSV dictionary.
# @return float, or None for an absent or unparseable value.
# @note Does not enforce finiteness or a zero-to-one range.
def get_confidence_value(row):
    value = get_first_non_empty_value(row, CONFIDENCE_FIELDS)

    if not value:
        return None

    try:
        return float(value)
    except ValueError:
        return None


## @brief Parse the elapsed start offset from supported start-time columns.
# @param row Detection CSV dictionary.
# @return Offset in seconds as float, or None when absent or unparseable.
# @note Does not reject negative or nonfinite offsets.
def get_start_seconds_value(row):
    value = get_first_non_empty_value(row, START_FIELDS)

    if not value:
        return None

    try:
        return float(value)
    except ValueError:
        return None


## @brief Compute the lower boundary of the containing 15-second block.
# @param start_seconds Finite elapsed offset in seconds.
# @return Integer block start, calculated by rounding down.
# @exception ValueError Raised for NaN.
# @exception OverflowError Raised for infinity.
def get_block_start_seconds(start_seconds):
    return int(math.floor(start_seconds / BLOCK_SECONDS)) * BLOCK_SECONDS


## @brief Keep the first row per source, species, and 15-second block.
# @param input_path Merged CSV to read.
# @param output_path Deduplicated CSV path, opened with truncation.
# @return Tuple (number of kept rows, number of removed rows).
# @details Rows without a usable deduplication key pass through unchanged.
# Original offsets and confidence values are preserved; the highest-confidence
# row is not preferentially selected.
# @warning Failures can leave a partial output; writes are not atomic.
def write_deduped_csv(input_path, output_path):
    kept_rows = 0
    removed_rows = 0
    seen_keys = set()

    with open_reader(input_path) as input_handle:
        reader = csv.DictReader(input_handle)

        if reader.fieldnames is None:
            error(f"No CSV header in {input_path}")

        with open(output_path, "w", newline="", encoding="utf-8") as output_handle:
            writer = csv.DictWriter(
                output_handle,
                fieldnames=reader.fieldnames,
                extrasaction="ignore"
            )
            writer.writeheader()

            for row in reader:
                species = get_species_value(row)
                start_seconds = get_start_seconds_value(row)
                source_file = row.get("File", "").strip()

                if not species or start_seconds is None or not source_file:
                    writer.writerow(row)
                    kept_rows += 1
                    continue

                dedup_key = (
                    source_file,
                    get_block_start_seconds(start_seconds),
                    species,
                )

                if dedup_key in seen_keys:
                    removed_rows += 1
                    continue

                seen_keys.add(dedup_key)
                writer.writerow(row)
                kept_rows += 1

    return kept_rows, removed_rows


## @brief Merge inputs, write a deduplicated CSV, and print species statistics.
# @details Takes one CSV or directory argument. Species statistics describe the
# merged rows before deduplication, and existing output files are overwritten.
# @exception SystemExit Raised for invalid arguments or detected input failures.
def main():
    if len(sys.argv) != 2:
        print(
            f"Usage: {sys.argv[0]} <path>",
            file=sys.stderr
        )
        sys.exit(1)

    root_path = os.path.abspath(sys.argv[1])
    csv_files = find_csv_files(root_path)

    if not csv_files:
        error(f"No CSV files found under: {root_path}")

    print(f"Found {len(csv_files)} CSV file(s)")
    print()

    output_path = os.path.abspath(
        os.path.join(os.getcwd(), OUTPUT_FILENAME)
    )
    deduped_output_path = os.path.abspath(
        os.path.join(os.getcwd(), DEDUPED_OUTPUT_FILENAME)
    )
    fieldnames = collect_fieldnames(csv_files)
    species_totals = Counter()
    species_common_names = {}
    species_confidence_min = {}
    species_confidence_max = {}
    row_count = 0

    with open(output_path, "w", newline="", encoding="utf-8") as output_handle:
        writer = csv.DictWriter(
            output_handle,
            fieldnames=fieldnames,
            extrasaction="ignore"
        )
        writer.writeheader()

        for index, csv_file in enumerate(csv_files, start=1):
            print(f"[{index}/{len(csv_files)}] Reading:")
            print(csv_file)
            print()

            with open_reader(csv_file) as input_handle:
                reader = csv.DictReader(input_handle)

                if reader.fieldnames is None:
                    error(f"No CSV header in {csv_file}")

                for row in reader:
                    writer.writerow(row)
                    row_count += 1

                    species = get_species_value(row)
                    if not species:
                        continue

                    species_totals[species] += 1

                    common_name = get_common_name_value(row)
                    if common_name and species not in species_common_names:
                        species_common_names[species] = common_name

                    confidence = get_confidence_value(row)
                    if confidence is None:
                        continue

                    if species not in species_confidence_min:
                        species_confidence_min[species] = confidence
                        species_confidence_max[species] = confidence
                        continue

                    if confidence < species_confidence_min[species]:
                        species_confidence_min[species] = confidence

                    if confidence > species_confidence_max[species]:
                        species_confidence_max[species] = confidence

    deduped_row_count, duplicate_row_count = write_deduped_csv(
        output_path,
        deduped_output_path
    )

    print("Merge complete.")
    print(f"Output:          {output_path}")
    print(f"Deduped output:  {deduped_output_path}")
    print(f"Rows merged:     {row_count}")
    print(f"Rows kept:       {deduped_row_count}")
    print(f"Rows removed:    {duplicate_row_count}")
    print(f"Species found:   {len(species_totals)}")
    print()
    print("Species totals:")

    sorted_species = sorted(
        species_totals.items(),
        key=lambda item: (-item[1], item[0].lower())
    )
    species_width = max(
        [len("Species")] + [len(species) for species, _ in sorted_species]
    )
    common_name_width = max(
        [len("Common Name")]
        + [len(species_common_names.get(species, "n/a")) for species, _ in sorted_species]
    )

    header = (
        f"{'Count':>5}  {'Min Conf':>8}  {'Max Conf':>8}  "
        f"{'Species':<{species_width}}  {'Common Name':<{common_name_width}}"
    )
    print(header)
    print("-" * len(header))

    for species, count in sorted_species:
        min_conf = species_confidence_min.get(species)
        max_conf = species_confidence_max.get(species)
        min_text = "n/a" if min_conf is None else f"{min_conf:.3f}"
        max_text = "n/a" if max_conf is None else f"{max_conf:.3f}"
        common_name = species_common_names.get(species, "n/a")
        print(
            f"{count:>5}  {min_text:>8}  {max_text:>8}  "
            f"{species:<{species_width}}  {common_name:<{common_name_width}}"
        )


if __name__ == "__main__":
    main()
