# Script Index

This project takes recordings from a Song Meter Mini 2, processes them with Birda, and prepares the results so they can be sideloaded into BirdNET-Go.

## `birda_process_wav.py`

Purpose:

- Process one WAV recording with Birda.
- Locate the matching `*_Summary.txt` file.
- Derive latitude and longitude from the closest summary entry.
- Run Birda on the WAV file.
- Add location and timing metadata to the generated CSV.

Input:

- One WAV file path.
- A sibling `*_Summary.txt` file in the parent track directory.

Output:

- The Birda-generated CSV is updated in place.
- New columns are appended:
  - `lat`
  - `lon`
  - `detection_time`
  - `timezone`

## `birda_process_loop.py`

Purpose:

- Batch-process a directory of WAV files.
- Recursively find every `.wav` file under the supplied path.
- Run `birda_process_wav.py` for each recording.

Input:

- One directory path.

Output:

- Per-file progress on stdout.
- A final processed/failed count.

## `birda_process_csv.py`

Purpose:

- Merge Birda CSV files from a directory tree into one CSV.
- Produce a species summary report on stdout.
- Generate a second CSV that drops duplicate detections within the same 15-second block for the same species and source file.

Input schema:

- One path argument.
- The path may be a directory or a single `.csv` file.
- Input files are expected to be Birda CSVs produced by the WAV workflow, including the standard detection fields and the metadata fields added by `birda_process_wav.py`.
- The merger reads these fields when present:
  - Species identity columns: `Scientific name`, `scientific name`, `Species`, `species`, `Common name`, `common name`
  - Confidence columns: `Confidence`, `confidence`, `Score`, `score`
  - Metadata columns added by the WAV processor:
    - `lat`
    - `lon`
    - `detection_time`
    - `timezone`
  - Time field used for deduplication:
    - `Start (s)`

Output schema:

- A single merged CSV named `birda_merged.csv` in the current working directory.
- A second filtered CSV named `birda_merged_15s_dedup.csv` in the current working directory.
- The output header is the union of all input headers, preserving the first-seen order.
- All input rows are copied through unchanged in `birda_merged.csv`.
- `birda_merged_15s_dedup.csv` removes repeated rows for the same species in the same 15-second block within the same source file, while keeping different species in that block.
- The files are written with UTF-8 encoding and a standard CSV header row.

Stdout report:

- Total rows merged.
- Total rows kept after 15-second deduplication.
- Total rows removed by deduplication.
- Total unique species found.
- One line per species with:
  - species name
  - common name
  - total detections
  - minimum confidence
  - maximum confidence

## `birda_csv_to_birdnet.py`

Purpose:

- Convert `birda_merged_15s_dedup.csv` into a BirdNET-Pi SQLite database.
- Create a destination directory with `BirdNET-Pi/` and `BirdSongs/` subdirectories, populate `BirdNET-Pi/scripts/` with the constructed `birds.db`, and create BirdNET-Pi's extracted recording directory structure.

Input:

- Two arguments: an output directory path and a maximum number of records to import.
- `birda_merged_15s_dedup.csv` must exist in the current working directory.
- The converter validates the merged CSV has the expected columns.
- Zero for the maximum imports all records.
- Prompts whether to generate 15-second MP3s, then (when generating) whether to overwrite existing MP3s. Both default to no.
- MP3 generation requires `ffmpeg` with the `libmp3lame` encoder on PATH.
- Existing output directories can be reused. The database and SQL are rebuilt on each run; the overwrite prompt controls only MP3s.

Output:

- A BirdNET-Pi-compatible `birds.db` SQLite database in `BirdNET-Pi/scripts/` under the output directory, limited to the requested number of imported rows.
- The database contains a populated `detections` table and the standard lookup indexes.
- Latitude and longitude are stored in `Lat` and `Lon`. There is no dedicated sensor ID column.
- `birds.sql` is retained at the output root and used to load the database.
- Optional MP3s are stored in `BirdSongs/Extracted/By_Date/YYYY-MM-DD/Common_Name/`. Each starts at the exact CSV `Start (s)` offset in the WAV identified by `File`, with no rounding to a deduplication block. Relative WAV paths are resolved against the CSV directory.
- MP3 names follow `Common_Name-ConfidencePercent-YYYY-MM-DD-birdnet-HH:MM:SS.mp3`, using detection time, rounded confidence, spaces replaced by underscores, and apostrophes removed. Both SQL and database `File_Name` contain this MP3 basename, even when generation is declined.
- Naming and directory conventions match https://github.com/Nachtzuster/BirdNET-Pi/blob/main/scripts/utils/reporting.py and `utils/classes.py`. When distinct source recordings or offsets would share a filename, the converter uses BirdNET-Pi's supported `birdnet-RTSP_1-HH:MM:SS.mp3` form (incrementing the number as needed). These numbers distinguish collisions in CSV order; they are not recorder IDs. SQL uses the final disambiguated basename.
- Clips at the end of a recording are padded with silence to reach 15 seconds. Missing WAVs or encoding failures stop the run, retaining any existing clip being replaced.
- A clip beginning at or beyond the end of the WAV fails rather than creating an empty MP3. CSV `End (s)` does not set the clip duration; extraction always requests 15 seconds from `Start (s)`.
- The directory date and filename timestamp use `detection_time`, falling back to the timestamp in the WAV basename plus `Start (s)` when blank. WAV offsets are elapsed seconds and are not adjusted for timezone.

Example:

```bash
python3 birda_csv_to_birdnet.py /path/to/export 100
```

For the first observation in the current CSV, the generated clip is:

```text
/path/to/export/BirdSongs/Extracted/By_Date/2026-01-26/Pied_Currawong/Pied_Currawong-92-2026-01-26-birdnet-20:15:00.mp3
```

Its `File_Name` in both `birds.sql` and `birds.db` is `Pied_Currawong-92-2026-01-26-birdnet-20:15:00.mp3`. Audio comes from seconds 60 through 75 of the CSV's source WAV. Declining MP3 generation still writes the intended MP3 references but does not create audio files. Existing unrelated files are not removed.

Verification (2026-09-09):

- Run `python3 -m unittest -v test_birda_csv_to_birdnet.py`. Audio integration tests require FFmpeg and are skipped when it is unavailable.
- Seven tests passed, covering prompts, BirdNET-Pi naming and paths, midnight rollover, fractional offset accuracy, decoded 15-second duration, silence padding, SQL/file correspondence, filename collisions, overwrite/skip behavior, and preservation of existing clips after an encoding failure.
- A three-observation smoke test using the actual source WAV produced MP3s, SQL, and a database under `/tmp/birdnet-pi-mp3-smoke-20260909`.
- The full CSV imported 15,721 observations with 15,721 distinct filenames, including three disambiguated collisions. Replaying the retained SQL exactly reproduced the database. This check did not generate the full audio collection; its output is under `/tmp/birdnet-pi-full-csv-check-20260909`.
- FFmpeg was absent from the system PATH. Verification used an isolated FFmpeg 7.0.2 binary from the `imageio-ffmpeg` wheel under `/tmp/birdnet-mp3-test-deps/imageio_ffmpeg/binaries`. A normal run still requires FFmpeg installed on PATH, or this temporary directory prepended to PATH while it exists.

## Notes

- All scripts are intended to be run from the command line.
- Errors are printed to stderr and exit with a non-zero status.
