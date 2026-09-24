#!/usr/bin/env python3

## @file birda_csv_to_birdnet.py
# @brief Export merged Birda detections as BirdNET-Pi SQL, SQLite, and optional MP3s.
# @details Takes an output directory and maximum record count (zero means all).
# Reads birda_merged_15s_dedup.csv from the working directory. Prompts for MP3
# generation and replacement; birds.sql and birds.db are always rebuilt.
# The database schema follows the BirdNET-Pi detections table. When audio is
# requested, each imported observation gets a 15-second clip beginning at its
# exact Start (s) offset; short tails are padded with silence. Source WAV paths
# in the CSV are resolved relative to the CSV directory.
# @par Command line
# @code{.sh}
# python3 birda_csv_to_birdnet.py OUTPUT_DIRECTORY MAX_RECORDS
# @endcode
# @par Output tree
# The output contains BirdNET-Pi/scripts/birds.db, birds.sql, and
# BirdSongs/Extracted/By_Date/YYYY-MM-DD/Common_Name/*.mp3 when audio is
# generated. The companion staging script can turn these into an unambiguous
# BirdNET-Go import directory.
# @see birda_process_csv.py
# @see prepare_birdnet_go_import.py
# @see test_birda_csv_to_birdnet.py

import csv
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta


INPUT_FILENAME = "birda_merged_15s_dedup.csv"
OUTPUT_DB_NAME = "birds.db"
OUTPUT_SQL_NAME = "birds.sql"
CLIP_SECONDS = 15
BIRDNET_PI_DIR_NAME = "BirdNET-Pi"
BIRDNET_PI_SCRIPTS_DIR_NAME = "scripts"
BIRDSONGS_DIR_NAME = "BirdSongs"
REQUIRED_COLUMNS = (
    "Start (s)",
    "End (s)",
    "Scientific name",
    "Common name",
    "Confidence",
    "File",
    "lat",
    "lon",
    "detection_time",
    "timezone",
)

WAV_FILENAME_RE = re.compile(r"^([^_]+)_(\d{8})_(\d{6})\.wav$", re.IGNORECASE)


## @class Output
# @brief Provide optional ANSI styling for progress and error messages.
# @details Styling is enabled only on interactive terminals and is disabled when
# NO_COLOR is present in the environment.
class Output:
    """Small terminal-output helper with safe non-interactive fallbacks."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"

    ## @brief Detect whether terminal styling should be enabled.
    def __init__(self):
        self.enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ

    ## @brief Return text decorated with styles when styling is enabled.
    # @param text Value to convert to text.
    # @param styles ANSI sequences to apply.
    # @return Styled or unstyled string.
    def paint(self, text, *styles):
        if not self.enabled:
            return str(text)
        return "".join(styles) + str(text) + self.RESET

    ## @brief Print a styled section heading.
    # @param title Heading text.
    def section(self, title):
        print()
        print(self.paint(f"=== {title} ===", self.BOLD, self.BLUE))


OUTPUT = Output()


SCHEMA = """
CREATE TABLE detections (
    Date DATE,
    Time TIME,
    Sci_Name VARCHAR(100) NOT NULL,
    Com_Name VARCHAR(100) NOT NULL,
    Confidence FLOAT,
    Lat FLOAT,
    Lon FLOAT,
    Cutoff FLOAT,
    Week INT,
    Sens FLOAT,
    Overlap FLOAT,
    File_Name VARCHAR(100) NOT NULL,
    vocalization TEXT
);

CREATE INDEX detections_Com_Name ON detections (Com_Name);
CREATE INDEX detections_Sci_Name ON detections (Sci_Name);
CREATE INDEX detections_Date_Time ON detections (Date DESC, Time DESC);
"""

INSERT_SQL = (
    "INSERT INTO detections ("
    "Date, Time, Sci_Name, Com_Name, Confidence, Lat, Lon, Cutoff, Week, Sens, Overlap, File_Name, vocalization"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


## @brief Report an export failure to stderr and terminate.
# @param message Human-readable failure description.
# @exception SystemExit Always raised with exit status 1.
def error(message):
    print(OUTPUT.paint(f"ERROR: {message}", OUTPUT.RED, OUTPUT.BOLD), file=sys.stderr)
    sys.exit(1)


## @brief Ask a yes/no question, defaulting to no on an empty response.
# @param prompt Question text without the answer suffix.
# @return True for yes or False for no; invalid responses repeat the prompt.
# @exception SystemExit Raised when input ends before a response is received.
def ask_yes_no(prompt):
    while True:
        try:
            answer = input(f"{OUTPUT.paint(prompt, OUTPUT.CYAN)} [y/N]: ").strip().lower()
        except EOFError:
            error("No response received to the interactive prompt")
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print(OUTPUT.paint("Please answer yes or no.", OUTPUT.YELLOW))


## @brief Derive the directory and MP3 basename expected by BirdNET-Pi.
# @param birdsongs_dir Export's BirdSongs root directory.
# @param common_name Display name; spaces become underscores and apostrophes are removed.
# @param confidence Finite detection probability between zero and one.
# @param detection_time Observation datetime supplying the date and whole-second time.
# @param stream_id Optional numeric RTSP prefix used to distinguish filename collisions.
# @return Tuple (Extracted/By_Date/date/species directory, MP3 basename).
# @exception SystemExit Raised when the common name is unsafe as a directory component.
# @note This function derives paths only; it does not create directories or files.
def clip_location(birdsongs_dir, common_name, confidence, detection_time, stream_id=None):
    # Match BirdNET-Pi's Detection.common_name_safe and extract_detection.
    common_name_safe = common_name.replace("'", "").replace(" ", "_")
    if common_name_safe in ("", ".", "..") or any(
        char in common_name_safe for char in ("/", "\\", "\0")
    ):
        error(f"Common name cannot be used as a BirdNET-Pi directory: {common_name!r}")
    confidence_pct = round(round(confidence, 4) * 100)
    date = detection_time.strftime("%Y-%m-%d")
    time = detection_time.strftime("%H:%M:%S")
    stream_prefix = f"RTSP_{stream_id}-" if stream_id is not None else ""
    filename = f"{common_name_safe}-{confidence_pct}-{date}-birdnet-{stream_prefix}{time}.mp3"
    directory = os.path.join(birdsongs_dir, "Extracted", "By_Date", date, common_name_safe)
    return directory, filename


## @brief Encode 15 seconds from an exact WAV offset using FFmpeg and libmp3lame.
# @param source_file Existing source WAV path.
# @param clip_path Destination MP3 path whose parent directory must already exist.
# @param start_seconds Finite non-negative elapsed offset, not a rounded deduplication block.
# @param overwrite Whether to replace an existing destination.
# @return True when generated; False when an existing destination is retained.
# @exception SystemExit Raised for a missing source or handled encoding/write failure.
# @exception OSError Raised if temporary-file creation or cleanup fails.
# @details Pads short tails with silence and replaces a destination only after
# successful encoding. Existing destinations are not validated when skipped.
def extract_mp3(source_file, clip_path, start_seconds, overwrite):
    if os.path.exists(clip_path) and not overwrite:
        return False
    if not os.path.isfile(source_file):
        error(f"Source WAV not found: {source_file}")

    with tempfile.NamedTemporaryFile(
        dir=os.path.dirname(clip_path), suffix=".mp3", delete=False
    ) as handle:
        temp_path = handle.name
    try:
        # Accurate input seeking decodes/discards audio before the CSV offset.
        # Pad a short recording tail so every clip still lasts 15 seconds.
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-abort_on", "empty_output",
                "-y", "-ss", str(start_seconds), "-i", source_file,
                "-map", "0:a:0", "-vn", "-af", "apad",
                "-t", str(CLIP_SECONDS), "-c:a", "libmp3lame",
                "-q:a", "2", temp_path,
            ],
            check=True, capture_output=True, text=True,
        )
        os.replace(temp_path, clip_path)
    except subprocess.CalledProcessError as exc:
        error(f"MP3 extraction failed for {source_file} at {start_seconds}s: {exc.stderr.strip()}")
    except OSError as exc:
        error(f"Cannot generate MP3 {clip_path}: {exc}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return True


## @brief Open a UTF-8 detection CSV, accepting an optional byte-order mark.
# @param csv_path Input CSV path.
# @return Open text handle; the caller must close it.
# @exception SystemExit Raised when the input cannot be opened.
def open_csv(csv_path):
    try:
        return open(csv_path, "r", newline="", encoding="utf-8-sig")
    except OSError as exc:
        error(f"Cannot open CSV file {csv_path}: {exc}")


## @brief Count the CSV records that will be imported.
# @param csv_path Source detection CSV.
# @param max_records Positive import limit, or zero for all records.
# @return Number of records selected for import.
# @exception SystemExit Raised for an invalid or unreadable CSV header.
def count_records_to_process(csv_path, max_records):
    with open_csv(csv_path) as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            error(f"No CSV header in {csv_path}")

        record_count = sum(1 for _ in reader)

    return min(record_count, max_records) if max_records else record_count


## @brief Require the converter's expected detection and metadata columns.
# @param fieldnames Header names from csv.DictReader, or None for an empty input.
# @param csv_path Source path used in diagnostics.
# @exception SystemExit Raised for a missing header or required column.
# @note Checks headers only, not individual row completeness or value types.
def validate_columns(fieldnames, csv_path):
    if fieldnames is None:
        error(f"No CSV header in {csv_path}")

    missing = [field for field in REQUIRED_COLUMNS if field not in fieldnames]
    if missing:
        error(
            "Missing required CSV columns: "
            + ", ".join(missing)
        )


## @brief Parse a required numeric CSV field with contextual error reporting.
# @param value Text value to strip and convert.
# @param field_name Column label used in diagnostics.
# @param row_number CSV line number, counting the header as line one.
# @return Parsed float; callers must separately check range and finiteness.
# @exception SystemExit Raised for missing or unparseable values.
def parse_required_float(value, field_name, row_number):
    try:
        return float(value.strip())
    except (AttributeError, ValueError):
        error(f"Invalid {field_name} value on row {row_number}: {value!r}")


## @brief Parse optional numeric metadata, treating invalid text as missing.
# @param value Numeric text, blank text, or None.
# @return Parsed float, or None for absent or unparseable values.
# @note No coordinate range or finiteness validation is performed.
def parse_optional_float(value):
    if value is None:
        return None

    stripped = value.strip()
    if not stripped:
        return None

    try:
        return float(stripped)
    except ValueError:
        return None


## @brief Parse a recording-local start timestamp from a WAV basename.
# @param source_file Path named PREFIX_YYYYMMDD_HHMMSS.wav.
# @param row_number CSV line number used in diagnostics.
# @return Naive datetime representing the recording start.
# @exception SystemExit Raised for an invalid filename or calendar timestamp.
def parse_recording_start(source_file, row_number):
    filename = os.path.basename(source_file)
    match = WAV_FILENAME_RE.match(filename)

    if not match:
        error(
            f"Invalid File value on row {row_number}: {source_file!r}"
        )

    try:
        return datetime.strptime(
            match.group(2) + match.group(3),
            "%Y%m%d%H%M%S"
        )
    except ValueError:
        error(
            f"Invalid File timestamp on row {row_number}: {source_file!r}"
        )


## @brief Use explicit detection time or derive it from recording start plus offset.
# @param value ISO-formatted detection datetime; blank or None enables the fallback.
# @param source_file WAV path whose basename supplies the fallback recording start.
# @param start_seconds Elapsed detection offset in seconds.
# @param row_number CSV line number used in diagnostics.
# @return Parsed or derived datetime; no timezone conversion is performed.
# @exception SystemExit Raised for invalid explicit time or fallback filename.
# @note An explicit detection_time is not checked against the WAV offset.
def parse_detection_time(value, source_file, start_seconds, row_number):
    stripped = value.strip() if value is not None else ""

    if stripped:
        try:
            return datetime.fromisoformat(stripped)
        except ValueError:
            error(f"Invalid detection_time value on row {row_number}: {value!r}")

    recording_start = parse_recording_start(source_file, row_number)
    return recording_start + timedelta(seconds=start_seconds)


## @brief Serialize the staging database to a retained SQL file.
# @param conn SQLite connection containing the schema and committed observations.
# @param sql_path Destination SQL path; an existing file is replaced.
# @exception OSError Raised for filesystem failures.
# @details Writes through a fixed sibling .tmp path before replacing the SQL file.
# This replacement is not transactional with the later database replacement.
def write_sql_dump(conn, sql_path):
    temp_sql_path = sql_path + ".tmp"

    if os.path.exists(temp_sql_path):
        os.remove(temp_sql_path)

    with open(temp_sql_path, "w", encoding="utf-8") as handle:
        for line in conn.iterdump():
            handle.write(line)
            handle.write("\n")

    os.replace(temp_sql_path, sql_path)


## @brief Build a replacement SQLite database by executing the retained SQL.
# @param sql_path SQL script previously written by write_sql_dump.
# @param db_path Destination database path; an existing database is replaced on success.
# @exception sqlite3.Error Raised for SQL execution or database failures.
# @exception OSError Raised for filesystem failures.
# @note Loads the entire SQL file into memory and uses a fixed .tmp database path.
def load_database_from_sql(sql_path, db_path):
    temp_db_path = db_path + ".tmp"

    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    with open(sql_path, "r", encoding="utf-8") as handle:
        sql = handle.read()

    with sqlite3.connect(temp_db_path) as conn:
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript(sql)
        conn.commit()

    os.replace(temp_db_path, db_path)


## @brief Import detections, optionally extract audio, retain SQL, and load SQLite.
# @param csv_path Merged detection CSV; relative WAV paths resolve against its directory.
# @param db_path Destination birds.db path with an existing parent directory.
# @param sql_path Retained SQL path with an existing parent directory.
# @param birdsongs_dir Root for BirdNET-Pi's Extracted/By_Date audio hierarchy.
# @param max_records Positive row limit, or zero/None to import all rows.
# @param generate_mp3 Whether to encode audio for imported observations.
# @param overwrite_mp3 Whether existing audio destinations may be replaced.
# @param audio_stats Optional dict populated with final MP3 counts.
# @param progress_total Total records selected for import.
# @return Number of imported rows.
# @exception SystemExit Raised for detected CSV or audio validation failures.
# @exception sqlite3.Error Raised for database failures.
# @exception OSError Raised for unhandled filesystem failures.
# @details File_Name always stores the derived MP3 basename, even when encoding
# is disabled. Audio is written before the SQL and database are published.
# @warning Collision prefixes depend on CSV order and are not persisted as source IDs.
def build_database(
    csv_path, db_path, sql_path, birdsongs_dir, max_records,
    generate_mp3=False, overwrite_mp3=False, audio_stats=None, progress_total=0,
):
    row_count = 0
    clips_generated = 0
    clips_skipped = 0
    clip_sources = {}

    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript(SCHEMA)

        with open_csv(csv_path) as handle:
            reader = csv.DictReader(handle)
            validate_columns(reader.fieldnames, csv_path)

            os.makedirs(birdsongs_dir, exist_ok=True)

            for row_number, row in enumerate(reader, start=2):
                scientific_name = row["Scientific name"].strip()
                common_name = row["Common name"].strip()
                confidence = parse_required_float(row["Confidence"], "Confidence", row_number)
                if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    error(f"Confidence must be between zero and one on row {row_number}")
                start_seconds = parse_required_float(row["Start (s)"], "Start (s)", row_number)
                if not math.isfinite(start_seconds) or start_seconds < 0:
                    error(f"Start (s) must be finite and non-negative on row {row_number}")
                latitude = parse_optional_float(row["lat"])
                longitude = parse_optional_float(row["lon"])
                source_file = row["File"].strip()
                detection_time = parse_detection_time(
                    row["detection_time"],
                    source_file,
                    start_seconds,
                    row_number
                )
                if not scientific_name:
                    error(f"Blank Scientific name on row {row_number}")
                if not common_name:
                    error(f"Blank Common name on row {row_number}")
                if not source_file:
                    error(f"Blank File on row {row_number}")

                clip_dir, filename = clip_location(
                    birdsongs_dir, common_name, confidence, detection_time
                )
                os.makedirs(clip_dir, exist_ok=True)
                clip_path = os.path.join(clip_dir, filename)
                source_path = os.path.abspath(os.path.join(os.path.dirname(csv_path), source_file))
                source_key = (source_path, start_seconds)
                stream_id = 0
                # BirdNET-Pi supports an optional RTSP_<number>- source prefix.
                # Use it to distinguish simultaneous or overlapping recordings.
                while clip_path in clip_sources and clip_sources[clip_path] != source_key:
                    stream_id += 1
                    clip_dir, filename = clip_location(
                        birdsongs_dir, common_name, confidence, detection_time, stream_id
                    )
                    clip_path = os.path.join(clip_dir, filename)
                clip_sources[clip_path] = source_key

                if generate_mp3:
                    encode_started_at = time.perf_counter()
                    if extract_mp3(source_path, clip_path, start_seconds, overwrite_mp3):
                        clips_generated += 1
                        status = "created"
                    else:
                        clips_skipped += 1
                        status = "kept"
                    elapsed_seconds = time.perf_counter() - encode_started_at
                    print(
                        f"{f'MP3 {status}':<20} [{row_count + 1}/{progress_total}]: "
                        f"{OUTPUT.paint(filename, OUTPUT.DIM)} "
                        f"(took {elapsed_seconds:.2f}s)"
                    )

                conn.execute(
                    INSERT_SQL,
                    (
                        detection_time.strftime("%Y-%m-%d"),
                        detection_time.strftime("%H:%M:%S"),
                        scientific_name,
                        common_name,
                        confidence,
                        latitude,
                        longitude,
                        0.5,
                        detection_time.isocalendar().week,
                        1.25,
                        0.0,
                        filename,
                        None,
                    )
                )
                row_count += 1

                if max_records and row_count >= max_records:
                    break

        conn.commit()
        write_sql_dump(conn, sql_path)

    load_database_from_sql(sql_path, db_path)
    if audio_stats is not None:
        audio_stats.update(
            generated=clips_generated,
            kept=clips_skipped,
        )
    return row_count


## @brief Validate export arguments, prompt for audio options, and run the import.
# @details Reads the fixed input CSV in the working directory and prints output
# paths and row counts. Existing SQL and database files are rebuilt on every run.
# @exception SystemExit Raised for invalid arguments, unavailable input, or handled failures.
def main():
    if len(sys.argv) != 3:
        error("Args required: output directory, max records")

    output_dir = os.path.abspath(sys.argv[1])

    try:
        max_records = int(sys.argv[2])
    except ValueError:
        error(f"Invalid max records value: {sys.argv[2]!r}")

    if max_records < 0:
        error(f"Max records must be zero or greater: {max_records}")

    input_csv = os.path.abspath(INPUT_FILENAME)

    if not os.path.isfile(input_csv):
        error(f"Input CSV not found: {input_csv}")

    if os.path.exists(output_dir) and not os.path.isdir(output_dir):
        error(f"Output path is not a directory: {output_dir}")

    generate_mp3 = ask_yes_no("Generate 15-second MP3 files?")
    overwrite_mp3 = ask_yes_no("Overwrite existing MP3 files?") if generate_mp3 else False
    if generate_mp3 and shutil.which("ffmpeg") is None:
        error("MP3 generation requires ffmpeg with the libmp3lame encoder on PATH")

    progress_total = count_records_to_process(input_csv, max_records)
    print(f"Records to process: {progress_total}")

    os.makedirs(output_dir, exist_ok=True)

    birdnet_pi_dir = os.path.join(output_dir, BIRDNET_PI_DIR_NAME)
    birdnet_pi_scripts_dir = os.path.join(
        birdnet_pi_dir,
        BIRDNET_PI_SCRIPTS_DIR_NAME
    )
    birdsongs_dir = os.path.join(output_dir, BIRDSONGS_DIR_NAME)
    os.makedirs(birdnet_pi_scripts_dir, exist_ok=True)
    os.makedirs(birdsongs_dir, exist_ok=True)

    db_path = os.path.join(birdnet_pi_scripts_dir, OUTPUT_DB_NAME)
    sql_path = os.path.join(output_dir, OUTPUT_SQL_NAME)
    audio_stats = {}
    row_count = build_database(
        input_csv,
        db_path,
        sql_path,
        birdsongs_dir,
        max_records,
        generate_mp3=generate_mp3,
        overwrite_mp3=overwrite_mp3,
        audio_stats=audio_stats,
        progress_total=progress_total,
    )

    OUTPUT.section("Export complete")
    print(OUTPUT.paint("BirdNET-Pi database created.", OUTPUT.GREEN, OUTPUT.BOLD))
    print(f"Output directory: {output_dir}")
    print(f"BirdNET-Pi dir:   {birdnet_pi_dir}")
    print(f"Scripts dir:      {birdnet_pi_scripts_dir}")
    print(f"BirdSongs dir:    {birdsongs_dir}")
    print(f"Database file:    {db_path}")
    print(f"SQL file:         {sql_path}")
    print(f"Max records:      {max_records}")
    print(f"Rows imported:    {row_count}")
    if generate_mp3:
        print(f"MP3s generated:   {audio_stats['generated']}")
        print(f"MP3s kept:         {audio_stats['kept']}")


if __name__ == "__main__":
    main()
