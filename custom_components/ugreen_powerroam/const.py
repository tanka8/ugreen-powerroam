"""Constants for the UGREEN PowerRoam integration.

Endpoints reverse engineered from the com.powerroam.pps Android app by
proxying its traffic (mitmproxy) through a patched, self-signed build.
See ~/Claude/ugreen-capture/sanitised.jsonl for the raw capture this is
built from.
"""

DOMAIN = "ugreen_powerroam"

API_HOST = "hw-powerapi.ugpps.com"
API_BASE = f"https://{API_HOST}"
WS_URL_TEMPLATE = (
    f"wss://{API_HOST}:8089/app/device/websocket/{{user_id}}/{{device_name}}"
)

# The app resends this after every control action and, from the capture
# timing, roughly every ~13s while idle - it doubles as the subscribe
# message and a keepalive.
WS_KEEPALIVE_INTERVAL = 25

# Telemetry arrives about every 15s. If six consecutive updates fail to show
# up the connection is treated as dead even though the socket still looks open
# - a half-open WebSocket reads as healthy forever otherwise, which is exactly
# how the cloud transport was observed to fail silently for an hour.
WS_STALL_TIMEOUT = 90

# Reconnecting with the same expired token will fail forever, so after this
# many consecutive failures the client logs in again for a fresh one.
WS_RELOGIN_AFTER_FAILURES = 3

WS_BACKOFF_START = 5
WS_BACKOFF_MAX = 60

# Transport selection. The cloud path is the original one; "ble" talks to the
# unit directly over Bluetooth LE and needs no account, no internet and no
# WiFi - see protocol.py for the wire format.
CONF_TRANSPORT = "transport"
CONF_ADDRESS = "address"
TRANSPORT_CLOUD = "cloud"
TRANSPORT_BLE = "ble"

# BLE GATT layout, read off a real GS1200 rather than assumed. Note the service
# is NOT advertised - only 0000FFFF-style vendor data is - so discovery matches
# on the local name instead. ABF1 declares "write" (with response), not
# write-without-response as the app bundle suggested.
BLE_SERVICE_UUID = "0000abf0-0000-1000-8000-00805f9b34fb"
BLE_WRITE_UUID = "0000abf1-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_UUID = "0000abf2-0000-1000-8000-00805f9b34fb"

# Observed local name is "ugreen gs1200"; matched case-insensitively and by
# prefix so sibling models are picked up too.
BLE_NAME_PREFIX = "ugreen"

# The device pushes its whole status set roughly every 0.6s, which is far more
# often than any dashboard needs. State is coalesced and written to HA at most
# this often; a genuine change to a switch still lands immediately.
BLE_STATE_DEBOUNCE = 2.0

BLE_RECONNECT_BACKOFF_START = 5
BLE_RECONNECT_BACKOFF_MAX = 300

CONF_TOKEN = "token"
CONF_USER_ID = "user_id"
CONF_DEVICE_NAME = "device_name"
CONF_SN = "sn"

# Switch entities: (data key, name).
# switch_conpower ("U-Turbo"?) and the various *_set config keys were left
# out deliberately - only confirmed by watching a real toggle in the
# capture, not guessed.
SWITCHES = {
    "switch_ac": "AC Output",
    "switch_dc": "DC Output",
    "usb_sw": "USB Output",
    "lamp_sw": "Flashlight",
}

# Raw-value sensors for the fields this integration cannot interpret yet. Each
# shows its value exactly as the unit sent it, so someone with a different model
# can watch what moves when a load is plugged in or a setting is changed in the
# app, and report back. They are diagnostics, start disabled (see
# DISABLED_BY_DEFAULT_SENSORS), and are not something to build automations on.
#
# Bluetooth: opcode -> what the app calls the frame; the sensor shows the
# payload as hex. These are the opcodes protocol.py leaves unmapped, plus 0x0B
# and 0x0C, where the bytes that are mapped are only part of the payload and the
# voltage bytes read zero on the 1200W - a model that fills them in is exactly
# what these are for. An opcode a unit never sends stays unavailable.
BLE_RAW_FRAMES = {
    0x01: "Fault",
    0x0B: "AC",
    0x0C: "Ports",
    0x10: "Screen Brightness",
    0x11: "Screen Timeout",
    0x14: "DC Charge Current",
    0x15: "Low Battery Alarm",
    0x17: "Timer",
}
BLE_RAW_KEYS = {opcode: f"raw_frame_{opcode:02x}" for opcode in BLE_RAW_FRAMES}

# Cloud: the telemetry keys the README lists as reported but not exposed, with
# the label it gives them. The three timers and U-Turbo are the least certain.
CLOUD_RAW_FIELDS = {
    "bat_health_set": "Battery Preservation Setting",
    "low_sound_set": "Quiet Mode Setting",
    "bee_sound_set_key": "Key Tone Setting",
    "bee_sound_set_warning": "Warning Tone Setting",
    "display_bright_set": "Display Brightness Setting",
    "low_power_al_set": "Low Battery Alarm Setting",
    "time_shutdown": "Shutdown Timer time_shutdown",
    "time_dis_shutdown": "Shutdown Timer time_dis_shutdown",
    "timeoff_set": "Shutdown Timer timeoff_set",
    "timeoff_cap": "Shutdown Timer timeoff_cap",
    "timeoff_zoom": "Shutdown Timer timeoff_zoom",
    "switch_conpower": "U-Turbo (unconfirmed) switch_conpower",
}

# Sensor entities: key -> (name, unit, device_class, state_class, entity_category, icon)
# Left unit/device_class as None where the raw scale wasn't confirmed against
# a known reference value during capture - still useful for graphing/automations,
# just don't trust the absolute number as a calibrated physical unit yet.
SENSORS = {
    "battery_percentage": ("Battery", "%", "battery", "measurement", None, None),
    "bat_health": (
        "Battery Health",
        "%",
        None,
        "measurement",
        "diagnostic",
        "mdi:heart-pulse",
    ),
    "bat_cycle_num": (
        "Battery Cycle Count",
        None,
        None,
        "total_increasing",
        "diagnostic",
        "mdi:battery-sync",
    ),
    "bat_cap_remain": (
        "Battery Capacity Remaining (raw)",
        None,
        None,
        "measurement",
        "diagnostic",
        "mdi:battery",
    ),
    "discharge_remain_time": (
        "Discharge Time Remaining",
        "s",
        "duration",
        "measurement",
        None,
        "mdi:timer-sand",
    ),
    "charge_remain_time": (
        "Charge Time Remaining",
        "s",
        "duration",
        "measurement",
        None,
        "mdi:timer-sand",
    ),
    "discharge_pow": ("Total Output Power", "W", "power", "measurement", None, None),
    "ac_discharge_pow": ("AC Output Power", "W", "power", "measurement", None, None),
    "car_discharge_pow": ("DC Output Power", "W", "power", "measurement", None, None),
    "usb_discharge_pow": ("USB Output Power", "W", "power", "measurement", None, None),
    "charge_power_all": ("Input Power", "W", "power", "measurement", None, None),
    "ac_in_vol": (
        "AC Input Voltage",
        "V",
        "voltage",
        "measurement",
        "diagnostic",
        None,
    ),
    "ac_vol": ("AC Output Voltage", "V", "voltage", "measurement", "diagnostic", None),
    "dc_vol": ("DC Output Voltage", "V", "voltage", "measurement", "diagnostic", None),
    "bat_temp1": (
        "Battery Temperature 1",
        "°C",
        "temperature",
        "measurement",
        "diagnostic",
        None,
    ),
    "bat_temp2": (
        "Battery Temperature 2",
        "°C",
        "temperature",
        "measurement",
        "diagnostic",
        None,
    ),
    "inverter_temp1": (
        "Inverter Temperature 1",
        "°C",
        "temperature",
        "measurement",
        "diagnostic",
        None,
    ),
    "inverter_temp2": (
        "Inverter Temperature 2",
        "°C",
        "temperature",
        "measurement",
        "diagnostic",
        None,
    ),
    "device_fault2": (
        "Fault Code",
        None,
        None,
        None,
        "diagnostic",
        "mdi:alert-circle-outline",
    ),
    "work_mode": ("Work Mode (raw)", None, None, None, "diagnostic", "mdi:cog-outline"),
    **{
        f"cell_voltage_{n}": (
            f"Cell {n} Voltage",
            "mV",
            "voltage",
            "measurement",
            "diagnostic",
            "mdi:battery-outline",
        )
        for n in range(1, 8)
    },
    "ble_rssi": (
        "Bluetooth Signal Strength",
        "dBm",
        "signal_strength",
        "measurement",
        "diagnostic",
        None,
    ),
    "ble_last_frame_age": (
        "Bluetooth Last Frame Age",
        "s",
        "duration",
        "measurement",
        "diagnostic",
        "mdi:timer-outline",
    ),
    "energy_input": (
        "Input Energy",
        "kWh",
        "energy",
        "total_increasing",
        None,
        None,
    ),
    "energy_output": (
        "Total Output Energy",
        "kWh",
        "energy",
        "total_increasing",
        None,
        None,
    ),
    **{
        BLE_RAW_KEYS[opcode]: (
            f"Raw {label} Frame (0x{opcode:02X})",
            None,
            None,
            None,
            "diagnostic",
            "mdi:code-braces",
        )
        for opcode, label in BLE_RAW_FRAMES.items()
    },
    **{
        key: (f"{label} (raw)", None, None, None, "diagnostic", "mdi:code-braces")
        for key, label in CLOUD_RAW_FIELDS.items()
    },
}

# Readings about the Bluetooth link rather than the power station. They come
# from the Bluetooth stack, not from a frame: key -> the UgreenBleDevice
# attribute that holds the value.
BLE_LINK_SENSORS = {
    "ble_rssi": "rssi",
    "ble_last_frame_age": "last_frame_age",
}

# Cumulative energy, for the Energy dashboard: key -> the power reading it is
# integrated from. Neither transport reports an energy counter, so these are
# summed up in Home Assistant (see energy.py) and restored across restarts.
ENERGY_SENSORS = {
    "energy_input": "charge_power_all",
    "energy_output": "discharge_pow",
}

# The unit pushes a frame about every 0.6 seconds. If none has arrived for this
# long the link is treated as stalled even though the connection still looks
# open, and power is no longer integrated - a half-open connection would
# otherwise be counted as the last reading, held, for as long as it lasted.
BLE_FRAME_STALE_AFTER = 20

# Entities that exist to troubleshoot rather than to use. They are registered
# disabled, so nobody gets extra entities (and their history) they did not ask
# for; turn them on from the entity's settings.
DISABLED_BY_DEFAULT_SENSORS = {
    *BLE_LINK_SENSORS,
    *BLE_RAW_KEYS.values(),
    *CLOUD_RAW_FIELDS,
}

# Which sensors each transport can actually populate. Entities are only created
# for keys their transport can fill, so neither setup carries a permanently
# unavailable entity. Anything absent from a transport's set is simply not
# offered there.
BLE_SENSORS = {
    "battery_percentage",
    "charge_remain_time",
    "discharge_remain_time",
    "discharge_pow",
    "ac_discharge_pow",
    "car_discharge_pow",
    "usb_discharge_pow",
    "charge_power_all",
    "work_mode",
    "bat_temp1",
    "bat_temp2",
    "inverter_temp1",
    "inverter_temp2",
    "bat_health",
    "bat_cycle_num",
    "bat_cap_remain",
    *(f"cell_voltage_{n}" for n in range(1, 8)),
    *BLE_LINK_SENSORS,
    *BLE_RAW_KEYS.values(),
    *ENERGY_SENSORS,
}

# Per-cell voltages have no cloud equivalent - the WebSocket never reports them.
# They are one of the things local Bluetooth buys you. The link readings are
# about a Bluetooth connection, so the cloud has nothing to say about them, and
# the raw frames are Bluetooth opcodes.
BLE_ONLY_SENSORS = (
    {f"cell_voltage_{n}" for n in range(1, 8)}
    | set(BLE_LINK_SENSORS)
    | set(BLE_RAW_KEYS.values())
)

# The cloud transport is offered everything except what only Bluetooth can fill.
# BLE_ONLY_SENSORS used to be defined and never applied, so a cloud setup got
# seven cell-voltage sensors that could never have a value.
CLOUD_SENSORS = set(SENSORS) - BLE_ONLY_SENSORS

# Raw values for these keys come from the API in seconds. native stays in
# seconds (so history/statistics don't break); suggested_unit_of_measurement
# tells HA's frontend to display and store new stats in hours instead.
SUGGESTED_HOURS = {"discharge_remain_time", "charge_remain_time"}
