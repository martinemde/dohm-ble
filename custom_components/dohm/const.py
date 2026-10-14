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
#
# The service UUID is the primary HA discovery matcher. The captured
# advertisement had no local name; HA also keeps a name fallback for scanners
# that resolve one. Do not assume every backend supplies the same fields.
COMMAND_SERVICE_UUID = "00005600-d102-11e1-9b23-00025b005aa5"
CHARACTERISTIC_UUID = "00005601-d102-11e1-9b23-00025b005aa5"

# Originally reported by LightBlue as the device UUID. It is not the command
# service/characteristic used here; kept only as a historical research reference.
ADVERTISED_SERVICE_UUID = "aa114b7e-92cf-f378-b56d-5d6d1654404b"

# Resolved BLE name, e.g. "MARPAC_DOHMB2"; used as a discovery fallback. The
# suffix varies per unit and the name may be absent. CoreBluetooth UUID
# addresses are host-specific; do not copy them to a different host/backend.
LOCAL_NAME_PREFIX = "MARPAC_DOHM"

# Speed range accepted by the device: 1..10 (11 returns "Failed 03$").
MIN_SPEED = 1
MAX_SPEED = 10

# Command grammar: COMMAND[,ID[,VALUE]]$  e.g. b"S,0136C4,3$" sets speed; the
# device acknowledges accepted commands with b"OK$".
TERMINATOR = "$"


# Chosen when the device is added, and only ever a *suggestion*: the device
# registry applies it when it first creates the device and never again, so
# moving the Dohm to another area later in the UI is not overwritten on the
# next restart.
CONF_AREA = "area"


# Only one entity platform is loaded per device. The initial setup stores the
# choice in entry data; legacy entries without a choice remain media players.
CONF_ENTITY_TYPE = "entity_type"
ENTITY_TYPE_MEDIA_PLAYER = "media_player"
ENTITY_TYPE_FAN = "fan"
ENTITY_TYPES = (ENTITY_TYPE_MEDIA_PLAYER, ENTITY_TYPE_FAN)
DEFAULT_ENTITY_TYPE = ENTITY_TYPE_MEDIA_PLAYER
