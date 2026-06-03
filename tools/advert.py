#!/usr/bin/env python3
"""Dump full BLE advertisement data for the Dohm.

Prints exactly the fields a Home Assistant manifest matcher can use
(local_name, service_uuids, manufacturer_data, service_data), so we can tell
whether discovery should match on the name or on an advertised service UUID.

Hold the Dohm's top button to make it advertise, then run:
    uv run tools/advert.py
"""

from __future__ import annotations

import asyncio

from bleak import BleakScanner

SCAN_SECONDS = 25.0
NAME_PREFIX = "MARPAC_DOHM"
KNOWN_ADDRESS = "71593516-89C6-0838-32E5-45669D0AD266"
KNOWN_UUIDS = {
    "aa114b7e-92cf-f378-b56d-5d6d1654404b",
    "00005600-d102-11e1-9b23-00025b005aa5",
    "00005601-d102-11e1-9b23-00025b005aa5",
}


def looks_like_dohm(device, adv) -> bool:
    name = (device.name or adv.local_name or "").upper()
    return (
        name.startswith(NAME_PREFIX)
        or device.address == KNOWN_ADDRESS
        or bool({u.lower() for u in adv.service_uuids} & KNOWN_UUIDS)
    )


def dump(device, adv) -> None:
    print(f"\n=== {device.name or adv.local_name or '(no name)'} @ {device.address}"
          f"  ({adv.rssi} dBm) ===")
    print(f"  local_name:        {adv.local_name!r}")
    print(f"  service_uuids:     {adv.service_uuids}")
    print(f"  manufacturer_data: "
          f"{ {k: v.hex() for k, v in adv.manufacturer_data.items()} }")
    print(f"  service_data:      "
          f"{ {k: v.hex() for k, v in adv.service_data.items()} }")
    print(f"  tx_power:          {adv.tx_power}")


async def main() -> None:
    print(f"Scanning {SCAN_SECONDS:.0f}s — hold the Dohm's top button now ...")
    found = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)
    matches = [(d, a) for d, a in found.values() if looks_like_dohm(d, a)]

    if matches:
        for device, adv in matches:
            dump(device, adv)
    else:
        print("\nNo Dohm seen. Devices that advertised service UUIDs:")
        for device, adv in found.values():
            if adv.service_uuids:
                print(f"  {adv.rssi:>4} dBm  {device.address}  "
                      f"{device.name or adv.local_name or '(no name)'}  "
                      f"{adv.service_uuids}")

    print(f"\n(total {len(found)} devices seen)")


if __name__ == "__main__":
    asyncio.run(main())
