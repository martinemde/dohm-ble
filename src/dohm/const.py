"""Constants for the Marpac Dohm BLE protocol."""

# Advertised GATT service UUID. Useful for recognizing the device (e.g. a Home
# Assistant bluetooth matcher). This is what was originally mistaken for a
# peripheral address.
SERVICE_UUID = "aa114b7e-92cf-f378-b56d-5d6d1654404b"

# The single characteristic the device exposes: writable (with response) and
# supports notifications, but is NOT readable. State only ever arrives via
# notifications. (TI BLE module; UUID is from the SensorTag family.)
CHARACTERISTIC_UUID = "00005600-d102-11e1-9b23-00025b005aa5"

# BLE local name the device advertises, e.g. "MARPAC_DOHMB2". The suffix varies
# per unit, so match on this prefix for discovery. NOTE: the per-host
# CoreBluetooth/MAC address is NOT stable across machines — always rediscover by
# name (or service UUID), never by a hardcoded address.
LOCAL_NAME_PREFIX = "MARPAC_DOHM"

# Command grammar observed: COMMAND[,ID[,VALUE]]$  e.g. b"S,0136C4,3$" sets
# speed; the device acknowledges accepted commands with b"OK$".
TERMINATOR = "$"
