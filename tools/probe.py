#!/usr/bin/env python3
"""Interactive REPL to probe the Dohm BLE characteristic.

Scans for the device first (required for reliable connection on macOS /
CoreBluetooth), connects, subscribes to notifications, and lets you type
commands. Every notification is printed as it arrives.

Usage:
    uv run tools/probe.py --scan          # just list nearby BLE devices + exit
    uv run tools/probe.py                 # auto-find the Dohm by name, connect
    uv run tools/probe.py <ADDRESS>       # connect to a specific address/UUID

Hold the device's top button ~5s to make it connectable, then run this.

Tips:
  - A trailing '$' is added automatically if omitted. Prefix a line with ':'
    to send it raw. Type ':quit' (or Ctrl-D) to exit.
  - Once you know the device id (send 'i$'), commands look like 'S,<id>,3$'.
"""

from __future__ import annotations

import asyncio
import sys

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from dohm.const import LOCAL_NAME_PREFIX, TERMINATOR

SCAN_SECONDS = 8.0
NAME_HINTS = (LOCAL_NAME_PREFIX.lower(), "marpac", "dohm")


def on_notify(_sender: int, data: bytearray) -> None:
    print(f"  <- {bytes(data)!r}")


async def read_line(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


async def scan_devices() -> dict:
    print(f"Scanning {SCAN_SECONDS:.0f}s ...")
    found = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)
    rows = sorted(found.values(), key=lambda da: -(da[1].rssi or -999))
    print(f"Found {len(rows)} device(s):")
    for dev, adv in rows:
        name = dev.name or adv.local_name or "(no name)"
        print(f"  {adv.rssi:>4} dBm  {dev.address}  {name}")
    return found


def match_device(found: dict, identifier: str | None) -> BLEDevice | None:
    # 1) explicit address/UUID match
    if identifier:
        for dev, _adv in found.values():
            if dev.address.lower() == identifier.lower():
                return dev
    # 2) fall back to a name hint (marpac / dohm)
    for dev, adv in found.values():
        name = (dev.name or adv.local_name or "").lower()
        if any(hint in name for hint in NAME_HINTS):
            return dev
    return None


def describe_and_select(client: BleakClient):
    """Print the GATT table and return the write+notify characteristic."""
    target = None
    print("\nGATT services:")
    for service in client.services:
        print(f"  service {service.uuid}")
        for char in service.characteristics:
            props = ",".join(char.properties)
            print(f"    char {char.uuid}  handle={char.handle:#06x}  [{props}]")
            writable = {"write", "write-without-response"} & set(char.properties)
            if writable and "notify" in char.properties:
                target = char
    if target is None:
        print("\nNo write+notify characteristic found.")
    else:
        print(f"\nUsing command characteristic: {target.uuid} (handle "
              f"{target.handle:#06x})")
    return target


async def repl(client: BleakClient, char) -> None:
    await client.start_notify(char, on_notify)
    print("Subscribed to notifications. Type a command (':quit' to exit).")
    while True:
        try:
            line = await read_line("send> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == ":quit":
            break
        if line.startswith(":"):
            payload = line[1:].encode("utf-8")
        else:
            if not line.endswith(TERMINATOR):
                line += TERMINATOR
            payload = line.encode("utf-8")
        print(f"  -> {payload!r}")
        await client.write_gatt_char(char, payload, response=True)
        await asyncio.sleep(0.3)


async def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--scan":
        await scan_devices()
        return

    identifier = args[0] if args else None
    found = await scan_devices()
    device = match_device(found, identifier)
    if device is None:
        print(
            f"\nCould not find the device (looked for address {identifier!r} or a "
            f"name containing {NAME_HINTS}).\n"
            "Hold the top button ~5s to make it connectable, then retry. "
            "If it never appears in the scan, advertising is button-gated."
        )
        return

    print(f"\nConnecting to {device.address} ({device.name or 'unnamed'}) ...")
    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")
        char = describe_and_select(client)
        if char is None:
            return
        await repl(client, char)
    print("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
