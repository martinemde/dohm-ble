"""Constants for the Marpac Dohm BLE protocol."""

# Home Assistant integration identity.
DOMAIN = "dohm"
MANUFACTURER = "Yogasleep"  # formerly Marpac; the unit still advertises MARPAC_*
MODEL = "Dohm Connect"
MODEL_ID = "4000900"

# The GATT service that carries the command characteristic, and the single
# writable/notify characteristic itself (confirmed from the live GATT table).
# The characteristic is write + notify, NOT readable: state arrives only via
# notifications. (TI BLE module; UUIDs are from the SensorTag family.)
COMMAND_SERVICE_UUID = "00005600-d102-11e1-9b23-00025b005aa5"
CHARACTERISTIC_UUID = "00005601-d102-11e1-9b23-00025b005aa5"

# Seen in the device's advertisement but NOT in the GATT table. Kept because a
# Home Assistant bluetooth matcher can match on an advertised service UUID.
ADVERTISED_SERVICE_UUID = "aa114b7e-92cf-f378-b56d-5d6d1654404b"

# BLE local name the device advertises, e.g. "MARPAC_DOHMB2". The suffix varies
# per unit, so match on this prefix for discovery. NOTE: the per-host
# CoreBluetooth/MAC address is NOT stable across machines — always rediscover by
# name (or service UUID), never by a hardcoded address.
LOCAL_NAME_PREFIX = "MARPAC_DOHM"

# Speed range accepted by the device: 1..10 (11 returns "Failed 03$").
MIN_SPEED = 1
MAX_SPEED = 10

# Command grammar: COMMAND[,ID[,VALUE]]$  e.g. b"S,0136C4,3$" sets speed; the
# device acknowledges accepted commands with b"OK$".
TERMINATOR = "$"
