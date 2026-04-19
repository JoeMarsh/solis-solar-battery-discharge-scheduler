"""Read-back probe for SolisCloud inverter control values.

Read-only. Does not write to the inverter.

Usage:
    python read_inverter_schedule.py             # reads cid 103 + cid 6798
    python read_inverter_schedule.py --cid 103   # read a single cid

Endpoint shape note: SolisCloud's "atRead" interface is referenced in the
control-API workbook (Demo + cid 543/636 notes) as the canonical way to read
the current value of a control parameter, but the request body is not
formally documented in the public PDF. The shape used here
({"inverterSn": ..., "cid": ...}) matches community usage. If the response
looks empty/error, log the raw text and adjust the body shape.
"""
import argparse
import json
import requests

from script import (
    API_URL,
    INVERTER_SN,
    construct_string_to_sign,
    generate_hmac_sha1_signature,
    get_gmt_time,
    get_md5_digest,
    API_KEY,
    API_SECRET,
)

READ_PATH = "/v2/api/atRead"


def read_cid(cid: int) -> dict:
    body = json.dumps({"inverterSn": INVERTER_SN, "cid": cid})
    content_md5 = get_md5_digest(body)
    date = get_gmt_time()
    string_to_sign = construct_string_to_sign(content_md5, date, READ_PATH)
    signature = generate_hmac_sha1_signature(string_to_sign, API_SECRET)
    headers = {
        "Content-MD5": content_md5,
        "Content-Type": "application/json;charset=UTF-8",
        "Date": date,
        "Authorization": f"API {API_KEY}:{signature}",
    }
    response = requests.post(f"{API_URL}{READ_PATH}", data=body, headers=headers, timeout=30)
    print(f"cid {cid}: HTTP {response.status_code}")
    try:
        parsed = response.json()
        print(f"cid {cid}: response={json.dumps(parsed, ensure_ascii=False, indent=2)}")
        return parsed
    except ValueError:
        print(f"cid {cid}: raw body={response.text!r}")
        return {"_raw": response.text}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cid",
        type=int,
        default=None,
        help="Read a single cid. Default: probe cid 103 (schedule) and cid 6798 (optimized-earnings marker).",
    )
    args = parser.parse_args()

    if args.cid is not None:
        read_cid(args.cid)
        return

    print("=== cid 103 (Charge/discharge schedule) ===")
    read_cid(103)
    print()
    print("=== cid 6798 (optimized-earnings marker, expect 0xAA55 if newer params apply) ===")
    read_cid(6798)


if __name__ == "__main__":
    main()
