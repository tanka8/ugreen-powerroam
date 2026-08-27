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
}

# Raw values for these keys come from the API in seconds. native stays in
# seconds (so history/statistics don't break); suggested_unit_of_measurement
# tells HA's frontend to display and store new stats in hours instead.
SUGGESTED_HOURS = {"discharge_remain_time", "charge_remain_time"}
