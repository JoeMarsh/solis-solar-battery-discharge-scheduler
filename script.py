import base64
import hashlib
import hmac
import requests
from datetime import datetime
from email.utils import formatdate
import json
import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# Constants
API_URL = "https://www.soliscloud.com:13333"
API_KEY = os.getenv("API_KEY") or "your_api_key_here"
API_SECRET = os.getenv("API_SECRET") or "your_api_secret_here"
INVERTER_SN = os.getenv("INVERTER_SN") or "your_inverter_sn_here"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") or "your_discord_webhook_url_here"

# Dyness DL5.0 Battery Specifications
BATTERY_NOMINAL_CAPACITY = 400  # Ah (4 x 100Ah batteries)
BATTERY_OPERATING_VOLTAGE_MIN = 44.8  # V
BATTERY_OPERATING_VOLTAGE_MAX = 57.6  # V
BATTERY_MAX_DISCHARGE_CURRENT = 100  # A

DISCHARGE_SOC = 20
CHARGE_TIME_RANGE = "02:05-05:55"
CLEAR_DISCHARGE_VALUE = (
    f"100,0,{CHARGE_TIME_RANGE},00:00-00:00,"
    "0,0,00:00-00:00,00:00-00:00,"
    "0,0,00:00-00:00,00:00-00:00"
)

# Logging. One source of truth: file next to this script, auto-rotated.
# ~5MB max total (1MB current + 4 x 1MB rotated, gzipped after rotate).
# Also mirrors to stdout so manual runs show output in the terminal.
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solarSet.log")
log = logging.getLogger("solis")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=4, delay=True)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_fh)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_sh)

def send_discord_message(message):
    webhook_url = DISCORD_WEBHOOK_URL
    payload = {"content": message}
    try:
        requests.post(webhook_url, json=payload, timeout=10)
        log.info("Discord message sent")
    except Exception as e:
        log.warning("Failed to send Discord message: %s", e)

def get_md5_digest(body):
    return base64.b64encode(hashlib.md5(body.encode('utf-8')).digest()).decode('utf-8')

def get_gmt_time():
    return formatdate(timeval=None, localtime=False, usegmt=True)

def generate_hmac_sha1_signature(data, secret):
    signature = hmac.new(secret.encode('utf-8'), data.encode('utf-8'), hashlib.sha1).digest()
    return base64.b64encode(signature).decode('utf-8')

def construct_string_to_sign(content_md5, date, path):
    return f"POST\n{content_md5}\napplication/json\n{date}\n{path}"

def get_battery_soc():
    path = "/v1/api/inverterDetail"
    body = json.dumps({"sn": INVERTER_SN})
    content_md5 = get_md5_digest(body)
    date = get_gmt_time()
    string_to_sign = construct_string_to_sign(content_md5, date, path)
    signature = generate_hmac_sha1_signature(string_to_sign, API_SECRET)
    headers = {
        "Content-MD5": content_md5,
        "Content-Type": "application/json;charset=UTF-8",
        "Date": date,
        "Authorization": f"API {API_KEY}:{signature}",
    }
    url = f"{API_URL}{path}"
    response = requests.post(url, data=body, headers=headers, timeout=30)
    data = response.json()
    if data.get("success"):
        return data["data"].get("batteryCapacitySoc")
    else:
        log.error("Failed to retrieve SOC: %s", data.get("msg"))
        return None

def calculate_discharge_current(soc, time_hours):
    soc_to_discharge = soc - DISCHARGE_SOC  # Discharge down to DISCHARGE_SOC%
    discharge_current = int((soc_to_discharge / 100) * BATTERY_NOMINAL_CAPACITY / time_hours)  # in Amps
    return min(discharge_current, BATTERY_MAX_DISCHARGE_CURRENT)

def _send_control(cid, value):
    """POST a SolisCloud control command. Used for cid 103 (schedule), cid 56 (time-set), etc."""
    path = "/v2/api/control"
    body = json.dumps({
        "cid": cid,
        "inverterSn": INVERTER_SN,
        "value": value,
        "language": "2"
    })
    content_md5 = get_md5_digest(body)
    date = get_gmt_time()
    string_to_sign = construct_string_to_sign(content_md5, date, path)
    signature = generate_hmac_sha1_signature(string_to_sign, API_SECRET)
    headers = {
        "Content-MD5": content_md5,
        "Content-Type": "application/json;charset=UTF-8",
        "Date": date,
        "Authorization": f"API {API_KEY}:{signature}",
    }
    response = requests.post(f"{API_URL}{path}", data=body, headers=headers, timeout=30)
    try:
        responsejson = response.json()
    except ValueError:
        responsejson = {"_raw": response.text}
    log.info("cid %s write: HTTP %s | sent value=%r", cid, response.status_code, value)
    log.info("cid %s write: full response=%s", cid, json.dumps(responsejson, ensure_ascii=False))
    return responsejson

def set_inverter_parameters(soc, time_hours):
    discharge_current = calculate_discharge_current(soc, time_hours)

    # Calculate the discharge start time
    discharge_end_hour = 2  # Discharge ends at 02:00
    discharge_start_hour = 24 - time_hours + discharge_end_hour
    if discharge_start_hour >= 24:
        discharge_start_hour -= 24

    discharge_time_range = f"{discharge_start_hour:02d}:00-{discharge_end_hour:02d}:00"

    if discharge_current < 1:
        value = CLEAR_DISCHARGE_VALUE
    else:
        value = (
            f"100,{int(discharge_current)},{CHARGE_TIME_RANGE},{discharge_time_range},"
            "0,0,00:00-00:00,00:00-00:00,"
            "0,0,00:00-00:00,00:00-00:00"
        )

    send_discord_message(f"Discharge Amps: {discharge_current}, Discharge Time: {discharge_time_range}")
    return _send_control(103, value)

def clear_discharge_slots():
    log.info("Clearing discharge slots (charge window %s preserved)", CHARGE_TIME_RANGE)
    send_discord_message(f"Clearing discharge slots (charge window {CHARGE_TIME_RANGE} preserved)")
    response = _send_control(103, CLEAR_DISCHARGE_VALUE)
    data_list = response.get("data", [])
    if isinstance(data_list, list) and data_list:
        msg = data_list[0].get("msg", "No message").replace("<br>", "\n")
    else:
        msg = "No data or invalid format"
    log.info("Response: %s", msg)
    send_discord_message(f"Response: {msg}")
    return response

def sync_inverter_time():
    """Push the Pi's current local time to the inverter (cid 56). Inverter is on Irish local time."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("Syncing inverter time to %s (Pi local clock)", now_str)
    send_discord_message(f"Syncing inverter time to {now_str}")
    response = _send_control(56, now_str)
    data_list = response.get("data", [])
    if isinstance(data_list, list) and data_list:
        msg = data_list[0].get("msg", "No message").replace("<br>", "\n")
    else:
        msg = "No data or invalid format"
    log.info("Response: %s", msg)
    send_discord_message(f"Response: {msg}")
    return response

def manage_discharge(hours):
    soc = get_battery_soc()
    if soc is not None:
        log.info("Current Battery SOC: %s%%", soc)
        send_discord_message(f"Current Battery SOC: {soc}%")
        if soc > DISCHARGE_SOC:
            log.info("Setting inverter parameters...")
            response = set_inverter_parameters(soc, hours)
            data_list = response.get("data", [])
            if isinstance(data_list, list) and len(data_list) > 0:
                responsemsg = data_list[0].get("msg", "No message")
            else:
                responsemsg = "No data or invalid format"
            formatted_response_msg = responsemsg.replace("<br>", "\n")
            log.info("Response: %s", formatted_response_msg)
            send_discord_message(f"Response: {formatted_response_msg}")
        else:
            log.info("SOC is too low for discharge.")
            send_discord_message("SOC is too low for discharge.")
    else:
        log.error("Unable to retrieve SOC.")
        send_discord_message("Unable to retrieve SOC.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage battery discharge.")
    parser.add_argument("--hours", type=int, default=1, help="Discharge duration in whole hours (default: 1).")
    parser.add_argument("--clear", action="store_true", help="Clear discharge slots (preserves the charge window). Skips SOC check.")
    parser.add_argument("--sync-time", action="store_true", help="Push the Pi's current local time to the inverter (cid 56). One-shot.")
    args = parser.parse_args()

    if args.clear:
        mode = "--clear"
    elif args.sync_time:
        mode = "--sync-time"
    else:
        mode = f"--hours {args.hours}"
    log.info("=== run start: %s ===", mode)
    try:
        if args.clear:
            clear_discharge_slots()
        elif args.sync_time:
            sync_inverter_time()
        else:
            manage_discharge(args.hours)
        log.info("=== run end ===")
    except Exception:
        log.exception("Unhandled error during run")
        raise
