import base64
import hashlib
import hmac
import requests
from email.utils import formatdate
import json
import argparse
import os

# Constants
API_URL = "https://www.soliscloud.com:13333"
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
INVERTER_SN = os.getenv("INVERTER_SN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Dyness DL5.0 Battery Specifications
BATTERY_NOMINAL_CAPACITY = 400  # Ah
BATTERY_OPERATING_VOLTAGE_MIN = 44.8  # V
BATTERY_OPERATING_VOLTAGE_MAX = 57.6  # V
BATTERY_MAX_DISCHARGE_CURRENT = 100  # A
# BATTERY_RECOMMENDED_DISCHARGE_CURRENT = 50  # A

def send_discord_message(message):
    webhook_url = DISCORD_WEBHOOK_URL
    payload = {"content": message}
    try:
        requests.post(webhook_url, json=payload)
        print("Discord message sent!")
    except Exception as e:
        print(f"Failed to send Discord message: {e}")

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
    response = requests.post(url, data=body, headers=headers)
    data = response.json()
    if data.get("success"):
        return data["data"].get("batteryCapacitySoc")
    else:
        print("Failed to retrieve SOC:", data.get("msg"))
        return None

def calculate_discharge_current(soc, voltage, time_hours):
    soc_to_discharge = soc - 20  # Discharge down to 20% SOC
    discharge_current = int((soc_to_discharge / 100) * BATTERY_NOMINAL_CAPACITY / time_hours)  # in Amps
    return min(discharge_current, BATTERY_MAX_DISCHARGE_CURRENT)

def set_inverter_parameters(soc, voltage, time_hours):
    path = "/v2/api/control"
    cid = 103  # Command ID for setting inverter parameters

    # Calculate discharge current for the specified time period
    discharge_current = calculate_discharge_current(soc, voltage, time_hours)

    # Fixed time range for charging
    charge_time_range = "02:05-05:55"

    # Calculate the discharge start time
    discharge_end_hour = 2  # Discharge ends at 02:00
    discharge_start_hour = 24 - int(time_hours) + discharge_end_hour  # Adjust for hours spanning midnight
    if discharge_start_hour >= 24:
        discharge_start_hour -= 24  # Keep it within 24-hour clock range

    # Format time ranges correctly
    if discharge_start_hour < discharge_end_hour:  # Same day
        discharge_time_range = f"{discharge_start_hour:02d}:00-{discharge_end_hour:02d}:00"
    else:  # Spans midnight
        discharge_time_range = f"{discharge_start_hour:02d}:00-02:00"

    if discharge_current < 1:
        # No discharge current, set discharge range to 0
        value = f"100,0,{charge_time_range},00:00-00:00,0,0,00:00-00:00,00:00-00:00,0,0,00:00-00:00,00:00-00:00"
    else:
        # Construct the value string dynamically with fixed charging and dynamic discharging
        value = f"100,{int(discharge_current)},{charge_time_range},{discharge_time_range},0,0,00:00-00:00,00:00-00:00,0,0,00:00-00:00,00:00-00:00"

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
    url = f"{API_URL}{path}"
    response = requests.post(url, data=body, headers=headers)
    send_discord_message(f"Discharge Amps: {discharge_current}, Discharge Time: {discharge_time_range}")
    responsejson = response.json()
    return responsejson

def manage_discharge(hours):
    soc = get_battery_soc()
    voltage = (BATTERY_OPERATING_VOLTAGE_MIN + BATTERY_OPERATING_VOLTAGE_MAX) / 2  # Average voltage
    if soc is not None:
        print(f"Current Battery SOC: {soc}%")
        send_discord_message(f"Current Battery SOC: {soc}%")
        if soc > 20:
            print("Setting inverter parameters...")
            response = set_inverter_parameters(soc, voltage, hours)
            data_list = response.get("data", [])
            if isinstance(data_list, list) and len(data_list) > 0:
                responsemsg = data_list[0].get("msg", "No message")
            else:
                responsemsg = "No data or invalid format"
            formatted_response_msg = responsemsg.replace("<br>", "\n")
            print("Response:", formatted_response_msg)
            send_discord_message(f"Response: {formatted_response_msg}")
        else:
            print("SOC is too low for discharge.")
            send_discord_message("SOC is too low for discharge.")
    else:
        print("Unable to retrieve SOC.")
        send_discord_message("Unable to retrieve SOC.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage battery discharge.")
    parser.add_argument("--hours", type=float, default=1.0, help="Discharge duration in hours (default: 1 hour).")
    args = parser.parse_args()

    manage_discharge(args.hours)
