#!/usr/bin/env python3

## @file birda_process_loop.py
# @brief Recursively process WAV recordings through birda_process_wav.py.
# @details Accepts one directory argument, prompts once about existing CSVs,
# processes sorted WAV paths sequentially, and exits nonzero if any child fails.
# Existing per-recording result CSVs are skipped unless the user answers yes to
# the overwrite prompt. A failed recording does not stop later recordings from
# being attempted; the final exit status reports whether any child failed.
# @par Command line
# @code{.sh}
# python3 birda_process_loop.py /path/to/song-meter-export
# @endcode
# @see birda_process_wav.py

import os
import subprocess
import sys


PROCESSOR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "birda_process_wav.py"
    )
)


## @brief Report a batch setup failure to stderr and terminate.
# @param message Human-readable failure description.
# @exception SystemExit Always raised with exit status 1.
def error(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


## @brief Ask whether existing per-recording CSVs should be regenerated.
# @return True for yes; False for no, an empty response, or end of input.
# @note Invalid responses repeat the prompt.
def prompt_overwrite_existing_csvs():
    while True:
        try:
            answer = input("Overwrite existing CSVs? [y/N]: ").strip().lower()
        except EOFError:
            print()
            return False

        if answer in ("", "n", "no"):
            return False

        if answer in ("y", "yes"):
            return True

        print("Please answer yes or no.")


## @brief Discover WAVs and invoke the single-recording processor for each file.
# @details Uses the current Python interpreter and continues after child failures.
# The printed Processed count includes skipped recordings and failed attempts.
# @exception SystemExit Raised for invalid arguments or one or more failed children.
def main():
    if len(sys.argv) != 2:
        print(
            f"Usage: {sys.argv[0]} <directory>",
            file=sys.stderr
        )
        sys.exit(1)

    root_dir = os.path.abspath(sys.argv[1])

    if not os.path.isdir(root_dir):
        error(f"Directory does not exist: {root_dir}")

    if not os.path.isfile(PROCESSOR):
        error(f"Processor script not found: {PROCESSOR}")

    wav_files = []

    for current_dir, dirs, files in os.walk(root_dir):
        for filename in files:
            if filename.lower().endswith(".wav"):
                wav_files.append(
                    os.path.abspath(
                        os.path.join(current_dir, filename)
                    )
                )

    wav_files.sort()

    print(f"Found {len(wav_files)} WAV file(s)")
    overwrite_existing_csvs = prompt_overwrite_existing_csvs()
    print()

    failures = 0

    for index, wav_file in enumerate(wav_files, start=1):
        print(
            f"[{index}/{len(wav_files)}] Processing:"
        )
        print(wav_file)
        print()

        command = [
            sys.executable,
            PROCESSOR
        ]

        if not overwrite_existing_csvs:
            command.append("--skip-existing-csv")

        command.append(wav_file)

        result = subprocess.run(command)

        if result.returncode != 0:
            failures += 1
            print(
                f"WARNING: Processing failed for: {wav_file}",
                file=sys.stderr
            )

        print()

    print("Finished.")
    print(f"Processed: {len(wav_files)}")
    print(f"Failures:  {failures}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
