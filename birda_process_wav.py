#!/usr/bin/env python3

## @file birda_process_wav.py
# @brief Analyze one Song Meter WAV with Birda and enrich its detection CSV.
# @details Reads PREFIX_YYYYMMDD_HHMMSS.wav and the recorder's summary one
# directory above the WAV directory. Updates the sibling .BirdNET.results.csv
# with coordinates, recording-local detection times, and the host timezone.
# The Birda invocation uses the CPU, a 0.50 confidence threshold, and the
# recorder's latitude/longitude. The generated CSV is replaced atomically after
# its rows have been validated and enriched.
# @par Command line
# @code{.sh}
# python3 birda_process_wav.py [--skip-existing-csv] recording.wav
# @endcode
# @par Required recording layout
# A recording such as Track/Data/4952_20260126_201400.wav must have
# Track/4952_Summary.txt beside the Data directory. The summary must contain
# DATE, TIME, LAT, NS, LON, and EW columns.
# @see birda_process_loop.py

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta


MIN_CONFIDENCE = 0.5
MAX_SUMMARY_TIME_DIFFERENCE = 30
ORT_LIBRARY = "/opt/birda/lib/libonnxruntime.so"
BIRDA_PREFERRED = "/opt/birda/bin/birda"


## @brief Report a command-line failure to stderr and terminate.
# @param message Human-readable failure description.
# @exception SystemExit Always raised with exit status 1.
def error(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


## @brief Parse the WAV path and optional --skip-existing-csv flag.
# @return argparse.Namespace containing wav_file and skip_existing_csv.
# @exception SystemExit Raised for help or invalid command-line arguments.
def parse_args():
    parser = argparse.ArgumentParser(
        description="Run Birda on one WAV file and add metadata to its CSV."
    )
    parser.add_argument("wav_file")
    parser.add_argument(
        "--skip-existing-csv",
        action="store_true",
        help="Skip processing if the expected BirdNET results CSV already exists."
    )

    return parser.parse_args()


## @brief Locate Birda at the preferred installation path or on PATH.
# @return Executable path suitable for subprocess.run.
# @exception SystemExit Raised when no Birda executable is available.
def find_birda():
    if os.path.isfile(BIRDA_PREFERRED) and os.access(BIRDA_PREFERRED, os.X_OK):
        return BIRDA_PREFERRED

    found = shutil.which("birda")

    if found:
        return found

    error("Could not find birda executable")


## @brief Determine the host's IANA timezone using timedatectl or /etc/localtime.
# @return Timezone name such as Australia/Sydney.
# @exception SystemExit Raised when neither lookup supplies a timezone.
# @note This identifies the processing host's timezone, not the recorder's.
def get_system_timezone():
    """
    Return the machine's configured IANA timezone.

    On RHEL this will normally come from systemd/timedatectl,
    for example:

        Australia/Sydney
    """

    try:
        result = subprocess.run(
            [
                "timedatectl",
                "show",
                "-p",
                "Timezone",
                "--value"
            ],
            capture_output=True,
            text=True,
            check=True
        )

        timezone = result.stdout.strip()

        if timezone:
            return timezone

    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    #
    # Fallback: inspect /etc/localtime if it is a symlink
    # into /usr/share/zoneinfo.
    #

    try:
        localtime = os.path.realpath("/etc/localtime")
        zoneinfo_prefix = "/usr/share/zoneinfo/"

        if localtime.startswith(zoneinfo_prefix):
            timezone = localtime[len(zoneinfo_prefix):]

            if timezone:
                return timezone
    except OSError:
        pass

    error(
        "Could not determine system timezone. "
        "Check 'timedatectl status'."
    )


## @brief Parse the recorder prefix and local start time from a WAV basename.
# @param wav_file Path named PREFIX_YYYYMMDD_HHMMSS.wav; extension is case-insensitive.
# @return Tuple (recorder prefix, naive datetime recording_start).
# @exception SystemExit Raised for an invalid filename or calendar timestamp.
def parse_wav_filename(wav_file):
    """
    Expected filename:

        4952_20260126_201400.wav

    Returns:

        prefix = 4952
        recording_start = 2026-01-26 20:14:00
    """

    filename = os.path.basename(wav_file)

    match = re.match(
        r"^([^_]+)_(\d{8})_(\d{6})\.wav$",
        filename,
        re.IGNORECASE
    )

    if not match:
        error(
            f"Invalid WAV filename: {filename}\n"
            "Expected format: PREFIX_YYYYMMDD_HHMMSS.wav"
        )

    prefix = match.group(1)

    try:
        recording_start = datetime.strptime(
            match.group(2) + match.group(3),
            "%Y%m%d%H%M%S"
        )
    except ValueError as exc:
        error(f"Invalid recording date/time: {exc}")

    return prefix, recording_start


## @brief Locate PREFIX_Summary.txt one directory above the WAV directory.
# @param wav_file Recording path, normally Track/Data/recording.wav.
# @param prefix Recorder identifier parsed from the WAV basename.
# @return Existing summary file path.
# @exception SystemExit Raised when the expected summary does not exist.
def find_summary_file(wav_file, prefix):
    """
    Given:

        .../Track/Data/4952_20260126_201400.wav

    Find:

        .../Track/4952_Summary.txt
    """

    wav_directory = os.path.dirname(wav_file)
    parent_directory = os.path.dirname(wav_directory)

    summary_file = os.path.join(
        parent_directory,
        f"{prefix}_Summary.txt"
    )

    if not os.path.isfile(summary_file):
        error(f"Summary file not found: {summary_file}")

    return summary_file


## @brief Read coordinates from the closest summary entry within 30 seconds.
# @param summary_file UTF-8 CSV summary with DATE, TIME, LAT, NS, LON, and EW.
# @param recording_start Naive recording-local datetime parsed from the WAV.
# @return Tuple (signed latitude, signed longitude, matched datetime, difference in seconds).
# @exception SystemExit Raised for invalid metadata or no sufficiently close entry.
# @note Equidistant entries retain the first match in file order.
def read_location(summary_file, recording_start):
    """
    Find the summary entry closest to the recording start time.
    """

    best_row = None
    best_time = None
    best_difference = None

    try:
        summary_handle = open(
            summary_file,
            "r",
            newline="",
            encoding="utf-8-sig"
        )
    except OSError as exc:
        error(f"Cannot open summary file: {exc}")

    with summary_handle:
        reader = csv.DictReader(summary_handle)

        required = {
            "DATE",
            "TIME",
            "LAT",
            "NS",
            "LON",
            "EW"
        }

        if reader.fieldnames is None:
            error(f"No CSV header in {summary_file}")

        missing = required - set(reader.fieldnames)

        if missing:
            error(
                "Missing summary columns: "
                + ", ".join(sorted(missing))
            )

        for row in reader:
            try:
                summary_time = datetime.strptime(
                    f"{row['DATE'].strip()} "
                    f"{row['TIME'].strip()}",
                    "%Y-%b-%d %H:%M:%S"
                )
            except (ValueError, AttributeError):
                continue

            difference = abs(
                (summary_time - recording_start).total_seconds()
            )

            if best_difference is None or difference < best_difference:
                best_difference = difference
                best_row = row
                best_time = summary_time

    if best_row is None:
        error(f"No valid timestamps found in {summary_file}")

    if best_difference > MAX_SUMMARY_TIME_DIFFERENCE:
        error(
            "No summary entry close enough to recording start.\n"
            f"Recording start: {recording_start:%Y-%m-%d %H:%M:%S}\n"
            f"Nearest entry:   {best_time:%Y-%m-%d %H:%M:%S}\n"
            f"Difference:      {best_difference:.0f} seconds"
        )

    try:
        latitude = float(best_row["LAT"].strip())
        longitude = float(best_row["LON"].strip())
    except (ValueError, AttributeError):
        error("Invalid latitude or longitude in summary file")

    ns = best_row["NS"].strip().upper()
    ew = best_row["EW"].strip().upper()

    if ns == "S":
        latitude = -abs(latitude)
    elif ns == "N":
        latitude = abs(latitude)
    else:
        error(f"Invalid NS value: {ns}")

    if ew == "W":
        longitude = -abs(longitude)
    elif ew == "E":
        longitude = abs(longitude)
    else:
        error(f"Invalid EW value: {ew}")

    return latitude, longitude, best_time, best_difference


## @brief Format a detection datetime, preserving nonzero fractional seconds.
# @param value datetime to format without a timezone suffix.
# @return YYYY-MM-DD HH:MM:SS with fractional seconds when needed.
def format_detection_time(value):
    if value.microsecond:
        return value.strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        ).rstrip("0")

    return value.strftime("%Y-%m-%d %H:%M:%S")


## @brief Derive the sibling Birda results CSV path from a WAV path.
# @param wav_file Source recording path.
# @return Path with the WAV extension replaced by .BirdNET.results.csv.
def get_csv_file(wav_file):
    wav_base, _ = os.path.splitext(wav_file)

    return wav_base + ".BirdNET.results.csv"


## @brief Rewrite a results CSV with coordinates and derived detection timestamps.
# @param csv_file Existing Birda results CSV, replaced after successful processing.
# @param latitude Signed latitude in decimal degrees.
# @param longitude Signed longitude in decimal degrees.
# @param recording_start Naive recording-local datetime from the WAV basename.
# @param timezone IANA timezone label written to every row.
# @return Number of enriched detection rows.
# @exception SystemExit Raised for CSV validation failures.
# @exception OSError Raised for unhandled write or replacement failures.
# @note detection_time equals recording_start plus the row's Start (s).
# @warning Validation exits can leave the fixed .tmp file behind.
def add_metadata_to_csv(
    csv_file,
    latitude,
    longitude,
    recording_start,
    timezone
):
    """
    Add/fill:

        lat
        lon
        detection_time
        timezone

    detection_time is:

        recording_start + Start (s)

    Example:

        2026-01-26 20:14:33
        Australia/Sydney
    """

    temp_file = csv_file + ".tmp"

    try:
        source_handle = open(
            csv_file,
            "r",
            newline="",
            encoding="utf-8-sig"
        )
    except OSError as exc:
        error(f"Cannot open Birda CSV: {exc}")

    try:
        with source_handle:
            reader = csv.DictReader(source_handle)

            if reader.fieldnames is None:
                error(f"No CSV header in {csv_file}")

            #
            # Remove our generated fields if they already exist,
            # then append them in a predictable order.
            #

            original_fields = [
                field
                for field in reader.fieldnames
                if field not in (
                    "lat",
                    "lon",
                    "detection_time",
                    "timezone"
                )
            ]

            fieldnames = original_fields + [
                "lat",
                "lon",
                "detection_time",
                "timezone"
            ]

            with open(
                temp_file,
                "w",
                newline="",
                encoding="utf-8"
            ) as destination_handle:

                writer = csv.DictWriter(
                    destination_handle,
                    fieldnames=fieldnames,
                    extrasaction="ignore"
                )

                writer.writeheader()

                row_count = 0

                for row in reader:
                    try:
                        start_offset = float(row["Start (s)"])
                    except (KeyError, ValueError, TypeError):
                        error(
                            f"Invalid Start (s) value in {csv_file}"
                        )

                    detection_time = (
                        recording_start
                        + timedelta(seconds=start_offset)
                    )

                    row["lat"] = f"{latitude:.5f}"
                    row["lon"] = f"{longitude:.5f}"
                    row["detection_time"] = format_detection_time(
                        detection_time
                    )
                    row["timezone"] = timezone

                    writer.writerow(row)

                    row_count += 1

        os.replace(temp_file, csv_file)

        return row_count

    except Exception:
        if os.path.exists(temp_file):
            os.remove(temp_file)

        raise


## @brief Run single-WAV analysis and metadata enrichment from command-line arguments.
# @details Validates source metadata and dependencies, runs Birda synchronously,
# and enriches its CSV. --skip-existing-csv checks existence only.
# @exception SystemExit Raised for argument, metadata, dependency, or Birda failures.
def main():
    args = parse_args()

    wav_file = os.path.abspath(args.wav_file)

    if not os.path.isfile(wav_file):
        error(f"WAV file not found: {wav_file}")

    csv_file = get_csv_file(wav_file)

    if args.skip_existing_csv and os.path.isfile(csv_file):
        print(f"Skipping existing CSV: {csv_file}")
        return

    #
    # Determine the machine timezone once.
    #

    timezone = get_system_timezone()

    prefix, recording_start = parse_wav_filename(wav_file)

    summary_file = find_summary_file(
        wav_file,
        prefix
    )

    (
        latitude,
        longitude,
        summary_time,
        time_difference
    ) = read_location(
        summary_file,
        recording_start
    )

    birda = find_birda()

    if not os.path.isfile(ORT_LIBRARY):
        error(
            f"ONNX Runtime library not found: {ORT_LIBRARY}"
        )

    print(f"WAV:             {wav_file}")
    print(f"Recorder:        {prefix}")
    print(
        f"Recording start: "
        f"{recording_start:%Y-%m-%d %H:%M:%S}"
    )
    print(f"Timezone:        {timezone}")
    print(f"Summary:         {summary_file}")
    print(
        f"Summary entry:   "
        f"{summary_time:%Y-%m-%d %H:%M:%S}"
    )
    print(
        f"Time difference: "
        f"{time_difference:.0f} seconds"
    )
    print(f"Latitude:        {latitude:.5f}")
    print(f"Longitude:       {longitude:.5f}")
    print(f"Confidence:      {MIN_CONFIDENCE:.2f}")
    print()

    command = [
        birda,
        "--force",
        "--cpu",
        "--no-progress",
        "--no-csv-bom",
        "-f", "csv",
        "-c", str(MIN_CONFIDENCE),
        "--lat", str(latitude),
        "--lon", str(longitude),
        "--month", str(recording_start.month),
        "--day", str(recording_start.day),
        "--range-unmatched", "drop",
        wav_file
    ]

    env = os.environ.copy()
    env["ORT_DYLIB_PATH"] = ORT_LIBRARY

    print("Running:")
    print(
        " ".join(
            f'"{arg}"' if " " in arg else arg
            for arg in command
        )
    )
    print()

    result = subprocess.run(
        command,
        env=env
    )

    if result.returncode != 0:
        error(
            f"Birda exited with status {result.returncode}"
        )

    #
    # Example:
    #
    #   4952_20260126_201400.wav
    #
    # becomes:
    #
    #   4952_20260126_201400.BirdNET.results.csv
    #

    if not os.path.isfile(csv_file):
        error(
            "Birda completed but CSV output was not found:\n"
            f"{csv_file}"
        )

    row_count = add_metadata_to_csv(
        csv_file,
        latitude,
        longitude,
        recording_start,
        timezone
    )

    print()
    print("Analysis complete.")
    print(f"CSV:             {csv_file}")
    print(f"Detections:      {row_count}")
    print(
        f"Coordinates:     "
        f"{latitude:.5f}, {longitude:.5f}"
    )
    print(f"Timezone:        {timezone}")
    print("Detection times: added")


if __name__ == "__main__":
    main()
