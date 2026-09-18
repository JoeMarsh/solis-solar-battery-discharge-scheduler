"""Configure Solis overnight charging and SOC-based discharge, with readback."""
import argparse
import base64
from dataclasses import dataclass
from email.utils import formatdate
import hashlib
import hmac
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
API_URL = "https://www.soliscloud.com:13333"
# Pi configuration: copy your existing hardcoded values into these four fields.
# Populated fields take precedence; .env/environment are optional local fallbacks.
API_KEY = "your_api_key_here"
API_SECRET = "your_api_secret_here"
INVERTER_SN = "your_inverter_sn_here"
DISCORD_WEBHOOK_URL = "your_discord_webhook_url_here"  # Optional
BATTERY_NOMINAL_CAPACITY = 400  # Ah: four 100 Ah Dyness modules
BATTERY_MAX_DISCHARGE_CURRENT = 100
DISCHARGE_SOC = 20
CHARGE_SOC = 100
CHARGE_CURRENT = 100
CHARGE_TIME_RANGE = "02:05-05:55"
EMPTY_TIME = "00:00-00:00"
CHARGE_SWITCHES = tuple(range(5916, 5922))
DISCHARGE_SWITCHES = tuple(range(5922, 5928))
SLOT_FIELDS = (5946, 5948, 5928, 5964, 5967, 5965)
GRID_CHARGE_BIT = 1 << 5
BASE_MODE_BITS = (1 << 0) | (1 << 2) | (1 << 6)
log = logging.getLogger("solis")


# Solis returns these as HH:MM-HH:MM, but control submissions join the
# separate start/end fields with a comma (solis-sensor button.py/control_const.py).
TIME_SLOT_CIDS = {5946, 5949, 5952, 5955, 5958, 5961, 5964, 5968, 5972, 5976, 5980, 5987}


class SolisError(RuntimeError):
    pass


def load_env(path):
    """Load simple KEY=value .env files; exported values take precedence."""
    path = Path(path)
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip().isidentifier():
            raise SolisError(f"Invalid .env assignment on line {number}")
        value = value.strip()
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                raise SolisError(f"Invalid .env quoting on line {number}")
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key.strip(), value)


def get_md5_digest(body):
    return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode()


def get_gmt_time():
    return formatdate(localtime=False, usegmt=True)


def generate_hmac_sha1_signature(data, secret):
    return base64.b64encode(hmac.new(secret.encode(), data.encode(), hashlib.sha1).digest()).decode()


def construct_string_to_sign(content_md5, date, path):
    # Preserve the signing format verified against this account's live API.
    return f"POST\n{content_md5}\napplication/json\n{date}\n{path}"


@dataclass(frozen=True)
class Setting:
    value: str
    raw: str


def check_result(result):
    if not isinstance(result, dict) or str(result.get("code")) != "0" or result.get("success") is False:
        code = result.get("code", "missing") if isinstance(result, dict) else "invalid response"
        raise SolisError(f"SolisCloud rejected request (code {code})")
    data = result.get("data")
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for item in items:
        if not isinstance(item, dict):
            raise SolisError("Unexpected device result")
        if ("code" in item and str(item["code"]) != "0") or item.get("success") is False or item.get("errorMsg"):
            raise SolisError(f"SolisCloud device failure (code {item.get('code', 'unknown')})")


class SolisClient:
    def __init__(self, key, secret, serial, url="https://www.soliscloud.com:13333", session=None):
        if not all([key, secret, serial]) or any(str(v).startswith("your_") for v in [key, secret, serial]):
            raise SolisError("Set API_KEY, API_SECRET and INVERTER_SN at the top of script.py (or optionally in .env/environment)")
        self.key, self.secret, self.serial = key, secret, serial
        self.url = url.rstrip("/")
        self.session = session or requests.Session()
        self._last_call = 0.0

    def post(self, path, payload, *, read_only=False):
        # Read-only requests may retry. Never blindly retry an ambiguous write.
        attempts = 2 if read_only else 1
        for attempt in range(attempts):
            delay = 0.6 - (time.monotonic() - self._last_call)
            if delay > 0:
                time.sleep(delay)
            body = json.dumps(payload)
            date = get_gmt_time()
            digest = get_md5_digest(body)
            signature = generate_hmac_sha1_signature(construct_string_to_sign(digest, date, path), self.secret)
            headers = {"Content-MD5": digest, "Content-Type": "application/json;charset=UTF-8",
                       "Date": date, "Authorization": f"API {self.key}:{signature}"}
            self._last_call = time.monotonic()
            try:
                response = self.session.post(self.url + path, data=body, headers=headers, timeout=(10, 35))
                response.raise_for_status()
                result = response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(2)
                    continue
                raise SolisError(f"{path}: {type(exc).__name__}; {'read failed' if read_only else 'write outcome unknown'}") from None
            check_result(result)
            return result

    def read_setting(self, cid):
        result = self.post("/v2/api/atRead", {"inverterSn": self.serial, "cid": cid}, read_only=True)
        # Completed reads can contain a bogus orderId equal to the value. The
        # completed data payload takes precedence; never treat orderId as data.
        for attempt in range(5):
            data = result.get("data")
            if isinstance(data, dict) and str(data.get("needLoop", "false")).lower() == "false":
                if data.get("msg") is not None and data.get("yuanzhi") is not None:
                    return Setting(str(data["msg"]).strip(), str(data["yuanzhi"]).strip())
            if isinstance(data, list) and len(data) == 1:
                item = data[0]
                if str(item.get("cid")) == str(cid) and item.get("value") is not None:
                    if (cid == 636 or 5916 <= cid <= 5927) and item.get("yuanzhi") is None:
                        raise SolisError(f"CID {cid}: missing raw switch register; cannot preserve sibling bits")
                    return Setting(str(item["value"]).strip(), str(item.get("yuanzhi", item["value"])).strip())
            order = result.get("orderId")
            if not order:
                break
            time.sleep(2)
            completion = self.post("/v2/api/result", {"orderId": order}, read_only=True)
            # Some result responses contain only execution status. Obtain the
            # parameter again instead of interpreting that status as its value.
            result = self.post("/v2/api/atRead", {"inverterSn": self.serial, "cid": cid}, read_only=True)
            check_result(completion)
        raise SolisError(f"CID {cid}: no completed parameter value returned")

    def write_setting(self, cid, value, original):
        if cid in TIME_SLOT_CIDS:
            value = str(value).replace("-", ",")
        return self.post("/v2/api/control", {"inverterSn": self.serial, "cid": cid,
                        "value": str(value), "yuanzhi": original.raw, "language": "2"})

    def battery_soc(self):
        result = self.post("/v1/api/inverterDetail", {"sn": self.serial}, read_only=True)
        data = result.get("data")
        value = data.get("batteryCapacitySoc") if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise SolisError("Missing or invalid battery SOC; no schedule written")
        try:
            age = time.time() - int(data["dataTimestamp"]) / 1000
        except (KeyError, TypeError, ValueError):
            raise SolisError("Missing telemetry timestamp; cannot verify SOC freshness") from None
        if not -300 <= age <= 900:
            raise SolisError("Battery SOC telemetry is stale or future-dated; no schedule written")
        return value


def configure_logging():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(ROOT / "solarSet.log", maxBytes=1_000_000, backupCount=4, delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(stream)


def config_value(name):
    value = globals()[name]
    if value and not str(value).startswith("your_"):
        return value
    return os.getenv(name)


def client_from_config():
    return SolisClient(config_value("API_KEY"), config_value("API_SECRET"), config_value("INVERTER_SN"), API_URL)


def send_discord_message(message):
    url = config_value("DISCORD_WEBHOOK_URL")
    if not url or url.startswith("your_"):
        return
    try:
        requests.post(url, json={"content": message}, timeout=10).raise_for_status()
    except requests.RequestException:
        log.warning("Discord notification failed")  # Never log a webhook URL.


def integer(value):
    value = str(value).strip()
    return int(value, 16) if value.lower().startswith("0x") else int(value)


def calculate_discharge_current(soc, time_hours):
    if isinstance(time_hours, bool) or not isinstance(time_hours, int) or not 1 <= time_hours <= 20:
        raise ValueError("--hours must be a whole number from 1 to 20 (avoids the charge window)")
    if isinstance(soc, bool) or not isinstance(soc, (int, float)) or not math.isfinite(soc) or not 0 <= soc <= 100:
        raise ValueError("SOC must be a finite number between 0 and 100")
    return max(0, min(int((soc - DISCHARGE_SOC) / 100 * BATTERY_NOMINAL_CAPACITY / time_hours),
                      BATTERY_MAX_DISCHARGE_CURRENT))


def schedule_values(soc, hours, clear=False):
    current = calculate_discharge_current(soc, hours)
    if clear:
        current = 0
    start = (2 - hours) % 24
    return current, f"{start:02d}:00-02:00" if current else EMPTY_TIME


def same_value(actual, expected):
    try:
        return float(actual) == float(expected)
    except (TypeError, ValueError):
        return str(actual).strip() == str(expected).strip()


def read_snapshot(client):
    marker = client.read_setting(6798)
    flag = integer(marker.value) & 0xFFFF
    if flag != 0xAA55:
        raise SolisError(f"Six-slot firmware required; unsupported capability marker {marker.value}; no settings written")
    cids = (636,) + CHARGE_SWITCHES + DISCHARGE_SWITCHES + SLOT_FIELDS
    snapshot = {6798: marker}
    for cid in cids:
        log.info("Reading CID %s", cid)
        snapshot[cid] = client.read_setting(cid)
    mode = integer(snapshot[636].raw)
    if mode & BASE_MODE_BITS not in (1, 64):
        raise SolisError("Expected Self-Use or Feed-in Priority; refusing to change another operating mode")
    return snapshot


def desired_settings(snapshot, soc, hours, clear=False):
    current, discharge_time = schedule_values(soc, hours, clear)
    # New firmware schedules are enabled per slot. The legacy global TOU bit
    # is ignored on the tested HMI 5103 (98 reads back as 96); do not require it.
    mode = integer(snapshot[636].raw) | GRID_CHARGE_BIT
    values = {cid: "0" for cid in CHARGE_SWITCHES + DISCHARGE_SWITCHES}
    values.update({5946: CHARGE_TIME_RANGE, 5948: str(CHARGE_CURRENT), 5928: str(CHARGE_SOC),
                   5964: discharge_time, 5967: str(current), 5965: str(DISCHARGE_SOC),
                   5916: "1", 5922: "1" if current else "0", 636: str(mode)})
    return values


def store_snapshot(snapshot, desired, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    path = directory / (now.strftime("%Y%m%d-%H%M%S-%f") + ".json")
    path.write_text(json.dumps({"captured_at": now.isoformat(), "protocol": "six-slot",
                               "before": {str(cid): {"value": setting.value, "raw": setting.raw}
                                          for cid, setting in snapshot.items()},
                               "desired": desired}, indent=2), encoding="utf-8")
    return path


def write_verified(client, cid, value):
    original = client.read_setting(cid)
    if cid == 636 and (integer(original.raw) & ~GRID_CHARGE_BIT) != (integer(value) & ~GRID_CHARGE_BIT):
        raise SolisError("Operating mode changed before write; refusing to overwrite other mode bits")
    if same_value(original.raw if cid == 636 else original.value, value):
        return False
    log.info("Writing CID %s = %s", cid, value)
    client.write_setting(cid, value, original)
    # Acknowledgement alone is not success. Poll reads, never repeat the write.
    for attempt in range(3):
        time.sleep(1)
        actual = client.read_setting(cid)
        if same_value(actual.raw if cid == 636 else actual.value, value):
            return True
    raise SolisError(f"CID {cid}: readback did not match {value}; stopping further writes")


def verify_schedule(client, desired):
    for cid, expected in desired.items():
        actual = client.read_setting(cid)
        if not same_value(actual.raw if cid == 636 else actual.value, expected):
            raise SolisError(f"Final verification failed for CID {cid}; expected {expected}")


def apply_schedule(client, snapshot, desired):
    """Disable affected slots while programming; enable discharge only last."""
    mode_before = integer(snapshot[636].raw)
    if integer(client.read_setting(636).raw) != mode_before:
        raise SolisError("Operating settings changed during preflight; rerun to take a new snapshot")
    try:
        for cid in DISCHARGE_SWITCHES:
            write_verified(client, cid, "0")
        charge_changed = any(not same_value(client.read_setting(cid).value, desired[cid]) for cid in (5946, 5948, 5928))
        if charge_changed:
            write_verified(client, 5916, "0")
        for cid in CHARGE_SWITCHES[1:]:
            write_verified(client, cid, "0")
        for cid in SLOT_FIELDS:
            write_verified(client, cid, desired[cid])
        write_verified(client, 5916, "1")
        if integer(client.read_setting(636).raw) != mode_before:
            raise SolisError("Operating settings changed while configuring slots; stopping")
        write_verified(client, 636, desired[636])
        write_verified(client, 5922, desired[5922])
        verify_schedule(client, desired)
    except Exception:
        # Keep restored charging, but do not leave a newly enabled discharge
        # running after an incomplete update. Never restore stale schedules.
        try:
            write_verified(client, 5922, "0")
        except Exception:
            log.error("Could not verify discharge slot 1 is disabled after failure; check SolisCloud")
        raise


def manage_discharge(hours, *, clear=False, dry_run=False, client=None, snapshot_dir=None):
    calculate_discharge_current(0, hours)  # Validate before any requests.
    client = client or client_from_config()
    snapshot = read_snapshot(client)
    soc = 0 if clear else client.battery_soc()
    desired = desired_settings(snapshot, soc, hours, clear)
    mode_name = "Feed-in Priority" if integer(snapshot[636].raw) & 64 else "Self-Use"
    log.info("Schedule format: six-slot; preserve mode: %s; SOC: %s", mode_name, "not queried (--clear)" if clear else soc)
    log.info("Charge: %s, %s A, target %s%%", CHARGE_TIME_RANGE, CHARGE_CURRENT, CHARGE_SOC)
    current, window = schedule_values(soc, hours, clear)
    log.info("Discharge: %s, %s A, target %s%%", window, current, DISCHARGE_SOC)
    for cid, value in desired.items():
        old = snapshot[cid].raw if cid == 636 else snapshot[cid].value
        log.info("CID %s: %s -> %s%s", cid, old, value, " (unchanged)" if same_value(old, value) else "")
    if dry_run:
        log.info("DRY RUN: read-only; no inverter writes or notifications")
        return desired
    path = store_snapshot(snapshot, desired, snapshot_dir or ROOT / "snapshots")
    log.info("Saved settings before update: %s", path)
    apply_schedule(client, snapshot, desired)
    log.info("Verified six-slot schedule, slot enables, and operating-mode bits")
    return desired


def clear_discharge_slots(**kwargs):
    return manage_discharge(1, clear=True, **kwargs)


def sync_inverter_time(client, dry_run=False):
    now = datetime.now(ZoneInfo("Europe/Dublin")).strftime("%Y-%m-%d %H:%M:%S")
    original = client.read_setting(56)
    log.info("Inverter time: %s; Dublin time: %s", original.value, now)
    if dry_run:
        return
    client.write_setting(56, now, original)
    actual = datetime.strptime(client.read_setting(56).value, "%Y-%m-%d %H:%M:%S")
    local_now = datetime.now(ZoneInfo("Europe/Dublin")).replace(tzinfo=None)
    if abs((local_now - actual).total_seconds()) > 90:
        raise SolisError("Inverter clock verification failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=1, help="Discharge duration ending 02:00; 1-20 whole hours.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--clear", action="store_true", help="Disable discharge and restore the configured charge window; no SOC query.")
    modes.add_argument("--sync-time", action="store_true", help="Set the inverter clock to Europe/Dublin time.")
    parser.add_argument("--dry-run", action="store_true", help="Read settings and show the proposed update without writes or notifications.")
    parser.add_argument("--no-notify", action="store_true", help="Suppress Discord notifications.")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)
    configure_logging()
    log.info("=== run start: %s%s ===", "sync-time" if args.sync_time else "clear" if args.clear else f"hours={args.hours}", " (dry run)" if args.dry_run else "")
    notify = not args.dry_run and not args.no_notify
    try:
        load_env(args.env_file)
        if args.sync_time:
            sync_inverter_time(client_from_config(), args.dry_run)
        else:
            manage_discharge(args.hours, clear=args.clear, dry_run=args.dry_run)
        if notify:
            send_discord_message("Solis clock verified." if args.sync_time else "Solis charge/discharge schedule written and verified.")
        log.info("=== run complete ===")
        return 0
    except (SolisError, ValueError, OSError) as exc:
        log.error("Run failed: %s", exc)
        if notify:
            send_discord_message(f"Solis scheduler FAILED: {exc}. Check solarSet.log and the inverter settings.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
