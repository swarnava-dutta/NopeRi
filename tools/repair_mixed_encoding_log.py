"""Repair a log file that was appended to in two different encodings.

``Run Noperi.bat`` used to append run markers from cmd (ANSI) and run output
from PowerShell 5.1's ``Tee-Object`` (UTF-16LE) to the same file. The result is
a file that is roughly half NUL bytes and that no single decoder can read:
``logs/noperi_hidden.log`` ended up at 1.5 MB with ~725k NUL bytes.

The launcher now writes UTF-8 only (see ``run_noperi.ps1``), so new logs are
clean. This one-shot tool recovers the readable history from an old file.

Usage:
    python tools/repair_mixed_encoding_log.py logs/noperi_hidden.log
    python tools/repair_mixed_encoding_log.py logs/noperi_hidden.log --in-place

Without ``--in-place`` the repaired text is written next to the original as
``<name>.repaired.log`` and nothing is overwritten.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# The two writers are trivially separable once you know their shapes:
#
#   * cmd wrote only the run markers, in ANSI, e.g.
#       [23-05-2026 17:38:01.61] Starting Noperi with "...python.exe"
#     — printable ASCII, no NUL bytes.
#   * PowerShell wrote everything else as UTF-16LE.
#
# Anchoring on the marker pattern is far more reliable than guessing from NUL
# density: emoji in UTF-16LE are surrogate pairs whose four bytes contain no
# NUL at all, so a density test mis-slices exactly the lines we care about and
# renders them as CJK mojibake.
_MARKER_RE = re.compile(rb"\[\d{2}-\d{2}-\d{4} [\d:.]+\][ -~]*")


def _decode_utf16(chunk: bytes) -> str:
    """Decode a UTF-16LE region, tolerating a lost leading/trailing byte."""
    if not chunk:
        return ""
    for start in (0, 1):
        body = chunk[start:]
        body = body[: len(body) - (len(body) % 2)]
        if not body:
            continue
        try:
            return body.decode("utf-16-le")
        except UnicodeDecodeError:
            continue
    return chunk.decode("utf-16-le", errors="replace")


def repair(path: str) -> str:
    raw = open(path, "rb").read()

    pieces: list[str] = []
    cursor = 0
    for match in _MARKER_RE.finditer(raw):
        pieces.append(_decode_utf16(raw[cursor : match.start()]))
        # Markers are ASCII; cp1252 covers any stray high byte from cmd.
        pieces.append("\n" + match.group().decode("cp1252", errors="replace") + "\n")
        cursor = match.end()
    pieces.append(_decode_utf16(raw[cursor:]))

    text = "".join(pieces)

    # Strip leftover NULs / lone surrogates and normalise line endings.
    text = text.replace("\x00", "")
    text = re.sub(r"[\ud800-\udfff]", "", text)

    # cmd terminated each marker with a 2-byte "\r\n" that sits INSIDE the
    # neighbouring UTF-16LE region, so it decodes to a single stray glyph
    # (U+0A0D "਍" for \r\n, U+0D0A for \n\r depending on alignment). Drop those
    # and any other C1/format debris the boundary produced.
    text = text.translate({0x0A0D: None, 0x0D0A: None, 0xFEFF: None})
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line.strip()) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="log file to repair")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="overwrite the original (a .bak copy is kept)",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.path):
        print(f"No such file: {args.path}")
        return 1

    original_size = os.path.getsize(args.path)
    repaired = repair(args.path)

    if args.in_place:
        backup = f"{args.path}.bak"
        if not os.path.exists(backup):
            os.replace(args.path, backup)
        else:
            print(f"Backup already exists, leaving it untouched: {backup}")
        target = args.path
    else:
        base, ext = os.path.splitext(args.path)
        target = f"{base}.repaired{ext or '.log'}"

    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(repaired)

    print(f"Read    : {args.path} ({original_size:,} bytes)")
    print(f"Wrote   : {target} ({os.path.getsize(target):,} bytes, UTF-8)")
    print(f"Lines   : {repaired.count(chr(10)):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
