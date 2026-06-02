#!/usr/bin/env python3
"""Interactive REPL to probe the Dohm BLE characteristic.

Connects to the device, subscribes to notifications, and lets you type
commands to send. Every notification from the device is printed as it arrives,
so you can watch the request/response dialogue live.

Usage:
    uv run tools/probe.py                 # uses the macOS UUID from const.py
    uv run tools/probe.py <PERIPHERAL>    # override (macOS CoreBluetooth UUID)

Tips:
  - A trailing '$' is added automatically if you omit it (every command ends
    in '$'). Prefix a line with ':' to send it raw, exactly as typed.
  - Type ':quit' (or Ctrl-D) to exit.

Piggyback workflow: let the official app connect/unlock the device first, then
run this to discover which verbs actually change speed/power while it's unlocked.
"""

from __future__ import annotations

import asyncio
import sys

from bleak import BleakClient

from dohm.const import CHARACTERISTIC_UUID, MACOS_PERIPHERAL_UUID, TERMINATOR


def on_notify(_sender: int, data: bytearray) -> None:
    print(f"  <- {bytes(data)!r}")


async def read_line(prompt: str) -> str:
    """Read one line from stdin without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


async def main() -> None:
    address = sys.argv[1] if len(sys.argv) > 1 else MACOS_PERIPHERAL_UUID
    print(f"Connecting to {address} ...")

    async with BleakClient(address) as client:
        print(f"Connected: {client.is_connected}")
        await client.start_notify(CHARACTERISTIC_UUID, on_notify)
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
            await client.write_gatt_char(CHARACTERISTIC_UUID, payload, response=True)
            # Give the device a beat to answer before the next prompt.
            await asyncio.sleep(0.3)

    print("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
