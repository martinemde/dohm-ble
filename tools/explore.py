#!/usr/bin/env python3
"""Non-interactive end-to-end probe of the Dohm.

Scans, connects, dumps the GATT table, then runs a scripted sequence using the
real protocol module: learn the device id, read current state, sweep the speed
range, toggle power, and restore the original state. Prints a full transcript.

Usage:
    uv run tools/explore.py

Hold the top button ~5s first if the device isn't already connectable.
"""

from __future__ import annotations

import asyncio

from bleak import BleakClient, BleakScanner

from custom_components.dohm import protocol
from custom_components.dohm.const import LOCAL_NAME_PREFIX

SCAN_SECONDS = 8.0
REPLY_TIMEOUT = 1.5


async def find_device():
    found = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)
    for dev, adv in found.values():
        name = (dev.name or adv.local_name or "")
        if name.upper().startswith(LOCAL_NAME_PREFIX):
            return dev
    return None


def select_char(client):
    for service in client.services:
        for char in service.characteristics:
            writable = {"write", "write-without-response"} & set(char.properties)
            if writable and "notify" in char.properties:
                return char
    return None


async def main() -> None:
    print(f"Scanning {SCAN_SECONDS:.0f}s for {LOCAL_NAME_PREFIX}* ...")
    device = await find_device()
    if device is None:
        print("Device not found. Hold the top button ~5s and retry.")
        return
    print(f"Found {device.name} @ {device.address}")

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")

        char = select_char(client)
        print("\nGATT services:")
        for service in client.services:
            for c in service.characteristics:
                mark = " <== command char" if c is char else ""
                print(f"  {c.uuid}  handle={c.handle:#06x}  "
                      f"[{','.join(c.properties)}]{mark}")
        if char is None:
            print("No write+notify characteristic found.")
            return

        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_notify(_sender, data: bytearray) -> None:
            queue.put_nowait(bytes(data))

        await client.start_notify(char, on_notify)

        async def send(payload: bytes, note: str = "") -> bytes | None:
            while not queue.empty():
                queue.get_nowait()
            print(f"  -> {payload.decode():<16} {note}")
            await client.write_gatt_char(char, payload, response=True)
            try:
                reply = await asyncio.wait_for(queue.get(), REPLY_TIMEOUT)
            except asyncio.TimeoutError:
                print("     <- (no reply)")
                return None
            try:
                print(f"     <- {reply.decode():<16} {protocol.parse(reply)}")
            except ValueError:
                print(f"     <- {reply!r} (unparsed)")
            return reply

        print("\n--- learn device id ---")
        reply = await send(protocol.query_id(), "query id")
        if reply is None:
            print("No id reply; aborting.")
            return
        device_id = protocol.parse(reply).value
        print(f"  device id = {device_id}")

        print("\n--- current state ---")
        power0 = await send(protocol.query_power(device_id), "query power")
        speed0 = await send(protocol.query_speed(device_id), "query speed")

        print("\n--- speed sweep 1..8 ---")
        for n in range(1, 9):
            await send(protocol.set_speed(device_id, n), f"set speed {n}")
            await send(protocol.query_speed(device_id), "read back")
        print("\n--- speed 0 (valid or no-op?) ---")
        await send(protocol.set_speed(device_id, 0), "set speed 0")
        await send(protocol.query_speed(device_id), "read back")

        print("\n--- power toggle ---")
        await send(protocol.set_power(device_id, False), "power off")
        await send(protocol.query_power(device_id), "read back")
        await send(protocol.set_power(device_id, True), "power on")
        await send(protocol.query_power(device_id), "read back")

        print("\n--- restore original state ---")
        if speed0 is not None:
            orig_speed = protocol.parse(speed0).speed
            await send(protocol.set_speed(device_id, orig_speed),
                       f"restore speed {orig_speed}")
        if power0 is not None:
            orig_on = protocol.parse(power0).on
            await send(protocol.set_power(device_id, orig_on),
                       f"restore power {'on' if orig_on else 'off'}")

        await client.stop_notify(char)
    print("\nDone. Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
