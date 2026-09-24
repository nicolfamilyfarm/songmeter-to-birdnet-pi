#!/usr/bin/env python3
## @file prepare_birdnet_go_import.py
# @brief Stage a BirdNET-Pi database and audio tree for BirdNET-Go import.
# @details BirdNET-Go expects the selected audio source directory itself to
# contain Extracted/By_Date. This utility creates a new, self-contained staging
# directory with BirdNET-Pi/scripts/birds.db and BirdSongs/Extracted/By_Date.
# It checks every detection's Date, Com_Name, and File_Name against the source
# audio tree before copying. Missing clips are reported but do not prevent the
# staging tree from being created.
# @par Command line
# @code{.sh}
# python3 prepare_birdnet_go_import.py DATABASE AUDIO_ROOT OUTPUT_DIRECTORY
# @endcode
# @par Safety rules
# DATABASE must be an existing SQLite database containing detections. AUDIO_ROOT
# must contain Extracted/By_Date. OUTPUT_DIRECTORY must not exist with contents;
# this prevents an accidental merge into an unrelated export.
# @warning The script copies the database and the complete Extracted tree. It
# does not delete or modify the source files.


import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path


## @brief Print a fatal staging error and terminate.
# @param message Human-readable diagnostic.
# @exception SystemExit Always raised with status 1.
def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


## @brief Validate a database-derived path component before joining it.
# @param value Candidate date, species directory, or filename component.
# @param label Component name used in diagnostics.
# @return The original component as a string.
# @exception SystemExit Raised for blank, dot-directory, or slash-containing values.
def safe_component(value, label):
    value = str(value)
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        die(f"unsafe {label} value in database: {value!r}")
    return value


## @brief Find one detection's expected MP3 below the source audio root.
# @param audio_root BirdSongs directory containing Extracted/By_Date.
# @param date Detection date stored in the database.
# @param common_name Detection common name stored in the database.
# @param file_name MP3 basename stored in the database.
# @return Existing clip Path, or None when it is absent.
# @exception SystemExit Raised when a database value is unsafe as a path component.
# @details Checks the exact common-name directory first, then the historical
# space-to-underscore/apostrophe-removal spelling used by BirdNET-Pi.
def source_clip(audio_root, date, common_name, file_name):
    date = safe_component(date, "date")
    common_name = safe_component(common_name, "common name")
    file_name = safe_component(file_name, "file name")

    exact = audio_root / "Extracted" / "By_Date" / date / common_name / file_name
    if exact.is_file():
        return exact

    fallback_name = common_name.replace(" ", "_").replace("'", "")
    if fallback_name != common_name:
        fallback = audio_root / "Extracted" / "By_Date" / date / fallback_name / file_name
        if fallback.is_file():
            return fallback
    return None


## @brief Read the fields needed to verify every database audio reference.
# @param database Readable SQLite birds.db path.
# @return List of (Date, Com_Name, File_Name) tuples.
# @exception SystemExit Raised when the database cannot be opened or lacks the
# detections table/columns.
def read_rows(database):
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        die(f"cannot open database: {exc}")

    try:
        rows = connection.execute(
            "SELECT Date, Com_Name, File_Name FROM detections"
        ).fetchall()
    except sqlite3.Error as exc:
        die(f"cannot read detections table: {exc}")
    finally:
        connection.close()
    return rows


## @brief Parse arguments, validate inputs, copy the database and audio tree.
# @exception SystemExit Raised for invalid arguments, missing inputs, a nonempty
# output directory, or an unreadable database.
# @details Prints counts for checked, found, and missing clips. Missing clips are
# warnings because BirdNET-Go can still import the corresponding detections.
def main():
    parser = argparse.ArgumentParser(
        description="Stage a BirdNET-Pi database and audio tree for BirdNET-Go."
    )
    parser.add_argument("database", type=Path, help="source BirdNET-Pi birds.db")
    parser.add_argument("audio_root", type=Path, help="source BirdSongs directory")
    parser.add_argument("output", type=Path, help="new staging directory")
    args = parser.parse_args()

    database = args.database.resolve()
    audio_root = args.audio_root.resolve()
    output = args.output.resolve()

    if not database.is_file():
        die(f"database not found: {database}")
    if not (audio_root / "Extracted" / "By_Date").is_dir():
        die(
            "audio root must contain Extracted/By_Date: "
            f"{audio_root}"
        )
    if output.exists() and any(output.iterdir()):
        die(f"output directory is not empty: {output}")

    rows = read_rows(database)
    missing = []
    for row_number, (date, common_name, file_name) in enumerate(rows, start=1):
        if source_clip(audio_root, date, common_name, file_name) is None:
            missing.append((row_number, date, common_name, file_name))

    staged_db = output / "BirdNET-Pi" / "scripts" / "birds.db"
    staged_audio = output / "BirdSongs"
    staged_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, staged_db)
    shutil.copytree(audio_root / "Extracted", staged_audio / "Extracted")

    print(f"Staged database: {staged_db}")
    print(f"Staged audio root: {staged_audio}")
    print(f"Detections checked: {len(rows)}")
    print(f"Audio clips found: {len(rows) - len(missing)}")
    print(f"Audio clips missing: {len(missing)}")

    if missing:
        print("First missing clips:")
        for row_number, date, common_name, file_name in missing[:10]:
            print(f"  row {row_number}: {date}/{common_name}/{file_name}")
        print(
            "The staging tree was created, but BirdNET-Go will import these "
            "detections without audio."
        )


if __name__ == "__main__":
    main()
