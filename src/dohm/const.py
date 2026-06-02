"""Constants for the Marpac Dohm BLE protocol."""

# The single characteristic the device exposes: writable (with response) and
# supports notifications, but is NOT readable. State only ever arrives via
# notifications. (TI BLE module; UUID is from the SensorTag family.)
CHARACTERISTIC_UUID = "00005600-d102-11e1-9b23-00025b005aa5"

# macOS CoreBluetooth peripheral identifier for THIS Mac. Not a real BLE MAC
# address and not portable to other hosts (e.g. the Home Assistant Pi), which
# must rediscover the device by its BLE local name instead.
MACOS_PERIPHERAL_UUID = "AA114B7E-92CF-F378-B56D-5D6D1654404B"

# Command grammar observed so far: COMMAND,VALUE$  e.g. b"S,03$" (speed 3).
# The device acknowledges accepted commands with b"OK$".
TERMINATOR = "$"
