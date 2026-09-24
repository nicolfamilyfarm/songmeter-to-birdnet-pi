# Song Meter to BirdNET-Go

This project converts recordings made by a Song Meter Mini 2 into a BirdNET-Pi-compatible SQLite database and audio directory that can be imported into [BirdNET-Go](https://github.com/tphakala/birdnet-go).

The workflow uses [Birda](https://github.com/tphakala/birda) to identify birds, then adapts Birda's CSV output to the database and directory conventions used by [BirdNET-Pi](https://github.com/Nachtzuster/BirdNET-Pi). The generated database is a mock BirdNET-Pi database: it is suitable as an import source, but it is not a live BirdNET-Pi installation.

## Workflow

Run the scripts in this order:

1. Analyze each WAV recording with Birda and add Song Meter location/time metadata.
2. Merge the per-recording CSV files and remove repeated detections in the same 15-second block.
3. Build a BirdNET-Pi-compatible `birds.db`, SQL dump, and optionally 15-second MP3 clips.
4. Stage the database and clips into a self-contained directory for BirdNET-Go.
5. Use BirdNET-Go's import workflow with the staged database and audio paths.

| Script | Purpose |
| --- | --- |
| `birda_process_wav.py` | Analyze one WAV with Birda and enrich its results CSV with latitude, longitude, local detection time, and timezone. |
| `birda_process_loop.py` | Recursively find WAV files and run the single-file processor for each one. |
| `birda_process_csv.py` | Merge Birda CSV files, print species totals, and produce a 15-second deduplicated CSV. |
| `birda_csv_to_birdnet.py` | Convert the deduplicated CSV into BirdNET-Pi SQL/SQLite output and optional MP3 clips. |
| `prepare_birdnet_go_import.py` | Copy the database and clips into a clean, self-contained BirdNET-Go staging directory. |

## Prerequisites

- Python 3.9 or newer. The scripts use only Python standard-library modules.
- A Song Meter export containing WAV recordings and the matching `PREFIX_Summary.txt` file.
- [Birda](https://github.com/tphakala/birda), installed as `birda` on `PATH` or at `/opt/birda/bin/birda`.
- The Birda ONNX Runtime library at `/opt/birda/lib/libonnxruntime.so`. Adjust the constant in `birda_process_wav.py` if Birda is installed elsewhere.
- `timedatectl`, or a `/etc/localtime` symlink into `/usr/share/zoneinfo`, so the processing host timezone can be recorded.
- `ffmpeg` with the `libmp3lame` encoder if audio clips are to be generated.
- [BirdNET-Go](https://github.com/tphakala/birdnet-go) for the final import.

No third-party Python package is required by this repository.

## Song Meter file layout

`birda_process_wav.py` expects the WAV basename to contain the recorder prefix and recording start time:

```text
PREFIX_YYYYMMDD_HHMMSS.wav
```

For example:

```text
Track/
├── 4952_Summary.txt
└── Data/
    └── 4952_20260126_201400.wav
```

The summary file must be a CSV with `DATE`, `TIME`, `LAT`, `NS`, `LON`, and `EW` columns. The closest valid summary row within 30 seconds of the WAV start is used. `NS` and `EW` are converted to signed decimal coordinates. The processing host's timezone is written to every detection row; timestamps are not converted to a different timezone.

## Step 1: Process recordings with Birda

To process one recording:

```bash
python3 birda_process_wav.py /path/to/Track/Data/4952_20260126_201400.wav
```

This runs Birda with a 0.50 confidence threshold and creates or updates the sibling file `4952_20260126_201400.BirdNET.results.csv`. The script appends or refreshes `lat`, `lon`, `detection_time` (recording start plus Birda's `Start (s)` offset), and `timezone`.

To process every WAV below a directory:

```bash
python3 birda_process_loop.py /path/to/song-meter-export
```

The loop asks once whether existing result CSVs should be overwritten. Failed files are reported after remaining files have been attempted, and the loop exits non-zero if any file failed.

## Step 2: Merge and deduplicate CSV files

Run the merger from the directory where you want the output files:

```bash
mkdir -p /path/to/work
cd /path/to/work
python3 /path/to/repository/birda_process_csv.py /path/to/song-meter-export
```

It creates `birda_merged.csv` (every input row, with the union of input headers) and `birda_merged_15s_dedup.csv` (the first row for each source file, species, and 15-second block).

Deduplication uses `(File, species, floor(Start (s) / 15) * 15)`. Different species remain in the same block. The original `Start (s)` offset is not rounded, so clip generation still begins at the actual detection offset. Rows missing a usable species, source file, or start offset pass through unchanged. The script also prints total rows, rows removed, unique species, common names, and minimum/maximum confidence values.

## Step 3: Create the mock BirdNET-Pi database

Run the converter from the directory containing `birda_merged_15s_dedup.csv`:

```bash
cd /path/to/work
python3 /path/to/repository/birda_csv_to_birdnet.py /path/to/export 0
```

The second argument is the maximum number of records. Use `0` to import all records, or a positive number for a smaller test export:

```bash
python3 /path/to/repository/birda_csv_to_birdnet.py /path/to/test-export 100
```

The converter validates required columns, asks whether to generate 15-second MP3 files, and asks whether existing clips may be overwritten. If MP3 generation is enabled, `ffmpeg` must be on `PATH`. SQL and SQLite output are rebuilt on each run; existing clips are only changed when overwrite is confirmed.

The output has this structure:

```text
/path/to/export/
├── BirdNET-Pi/
│   └── scripts/
│       └── birds.db
├── BirdSongs/
│   └── Extracted/By_Date/YYYY-MM-DD/Common_Name/
│       └── Common_Name-Confidence-YYYY-MM-DD-birdnet-HH:MM:SS.mp3
└── birds.sql
```

The database contains the BirdNET-Pi `detections` table and lookup indexes. `File_Name` values in SQL and SQLite match the generated MP3 basenames, including BirdNET-Pi-compatible collision suffixes when needed. If MP3 generation is declined, the database still contains intended `File_Name` values, but the referenced audio files are not created.

## Step 4: Stage the import directory

The staging helper makes the audio root unambiguous for BirdNET-Go:

```bash
python3 /path/to/repository/prepare_birdnet_go_import.py \
    /path/to/export/BirdNET-Pi/scripts/birds.db \
    /path/to/export/BirdSongs \
    /path/to/birdnet-go-import
```

It creates:

```text
/path/to/birdnet-go-import/
├── BirdNET-Pi/scripts/birds.db
└── BirdSongs/Extracted/By_Date/...
```

The output directory must be new or empty. The script checks every database reference and reports missing clips before copying. Missing audio does not abort staging, but BirdNET-Go will import those detections without audio. Source files are not modified.

## Step 5: Import into BirdNET-Go

Install and run [BirdNET-Go](https://github.com/tphakala/birdnet-go), then use its BirdNET-Pi/database import function. Select:

1. `birdnet-go-import/BirdNET-Pi/scripts/birds.db` as the database;
2. `birdnet-go-import/BirdSongs` as the audio source.

Select `BirdSongs`, not the nested `Extracted/By_Date` directory: the importer resolves the standard `Extracted/By_Date/<date>/<species>/<file>` hierarchy from that root. Review the import summary and verify several detections and clips before removing the staging directory. Consult the [BirdNET-Go documentation](https://github.com/tphakala/birdnet-go/wiki) for the version being used, since importer options can change between releases.

## Doxygen and man pages

The Python source contains Doxygen comments for each script, command-line entry point, and processing helper. With Doxygen installed, generate the man pages with:

```bash
make man
```

Generated man pages are written below `man/man3/`. Run `make man` to regenerate them, or `make clean-man` to remove the generated man directory.

## Limitations

- These scripts do not install Birda, BirdNET-Pi, BirdNET-Go, FFmpeg, or model files.
- `birda_process_wav.py` records the host timezone label; it does not perform timezone conversion.
- The deduplicator keeps the first matching row, not necessarily the highest-confidence row.
- The converter creates 15-second clips from `Start (s)` and pads short recording tails with silence.
- Source WAV files are never modified.

## Upstream projects

- [Birda](https://github.com/tphakala/birda) — offline bird-sound detection CLI.
- [BirdNET-Pi](https://github.com/Nachtzuster/BirdNET-Pi) — database and audio directory conventions used by the generated mock export.
- [BirdNET-Go](https://github.com/tphakala/birdnet-go) — target application for importing staged detections and clips.
