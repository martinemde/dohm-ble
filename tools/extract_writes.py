#!/usr/bin/env python3
"""Extract the Dohm BLE conversation from a PacketLogger capture.

Reads a `.pklg` (or any capture tshark understands) and prints the ordered
dialogue of ATT writes (app -> device) and notifications (device -> app),
decoded as UTF-8. This is the readable command transcript we use to reverse
the init/unlock handshake and the control verbs.

Usage:
    uv run tools/extract_writes.py path/to/capture.pklg
    uv run tools/extract_writes.py capture.pklg --handle 0x002a   # narrow

Requires `tshark` on PATH (Wireshark). Writes reference the characteristic by
numeric handle, not its UUID, so by default we show every ATT write/notification
with a printable payload; pass --handle once you know it to filter precisely.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

# ATT opcodes we care about, mapped to a human direction label.
ATT_OPCODES = {
    "0x12": ("->", "Write Request"),
    "0x52": ("->", "Write Command"),
    "0x1b": ("<-", "Notification"),
    "0x1d": ("<-", "Indication"),
}


def run_tshark(capture: str) -> list[str]:
    """Return tshark field rows for the ATT opcodes of interest."""
    display_filter = " || ".join(f"btatt.opcode == {op}" for op in ATT_OPCODES)
    cmd = [
        "tshark",
        "-r", capture,
        "-Y", display_filter,
        "-T", "fields",
        "-e", "frame.number",
        "-e", "frame.time_relative",
        "-e", "btatt.opcode",
        "-e", "btatt.handle",
        "-e", "btatt.value",
        "-E", "separator=\t",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"tshark failed:\n{result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def decode_hex(colon_hex: str) -> bytes:
    """Turn tshark's '53:2c:30:33:24' into raw bytes."""
    if not colon_hex:
        return b""
    return bytes(int(b, 16) for b in colon_hex.split(":"))


def as_text(data: bytes) -> str:
    """Printable rendering: real chars where possible, \\xNN otherwise."""
    out = []
    for byte in data:
        char = chr(byte)
        out.append(char if char.isprintable() else f"\\x{byte:02x}")
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", help="PacketLogger .pklg (or pcap) file")
    parser.add_argument(
        "--handle",
        help="only show writes/notifications for this ATT handle, e.g. 0x002a",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="include packets with empty/non-printable payloads (e.g. CCCD)",
    )
    args = parser.parse_args()

    if shutil.which("tshark") is None:
        sys.exit("tshark not found on PATH. Install Wireshark.")

    rows = run_tshark(args.capture)
    if not rows:
        sys.exit("No ATT writes or notifications found in this capture.")

    print(f"{'#':>5}  {'time':>9}  dir  {'handle':>7}  {'op':<13}  payload")
    print("-" * 72)

    for row in rows:
        parts = row.split("\t")
        # A single frame can carry more than one ATT PDU; tshark comma-joins
        # repeated fields. Take the first of each for a clean per-frame view.
        frame, rel_time, opcode, handle, value = (
            (parts + ["", "", "", "", ""])[:5]
        )
        opcode = opcode.split(",")[0]
        handle = handle.split(",")[0]
        value = value.split(",")[0]

        if args.handle and handle.lower() != args.handle.lower():
            continue

        arrow, label = ATT_OPCODES.get(opcode, ("?", opcode))
        data = decode_hex(value)
        printable = data and all(chr(b).isprintable() for b in data)
        if not args.all and not printable:
            continue

        rel = f"{float(rel_time):.3f}" if rel_time else ""
        text = as_text(data) if data else "(no value)"
        print(f"{frame:>5}  {rel:>9}  {arrow:>3}  {handle:>7}  {label:<13}  {text}")


if __name__ == "__main__":
    main()
