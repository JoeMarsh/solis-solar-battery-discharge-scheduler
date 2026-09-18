"""Read-only Solis schedule inspection. Loads .env; never sends control writes."""
import argparse
import json
import sys

from script import ROOT, client_from_config, load_env, read_snapshot
from script import SolisError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cid", type=int, action="append", help="Read one CID; repeat for multiple parameters.")
    parser.add_argument("--env-file", default=ROOT / ".env")
    args = parser.parse_args()
    try:
        load_env(args.env_file)
        client = client_from_config()
        if args.cid:
            protocol = None
            settings = {cid: client.read_setting(cid) for cid in args.cid}
        else:
            protocol = "six-slot"
            settings = read_snapshot(client)
        print(json.dumps({"protocol": protocol, "settings": {str(cid): {"value": item.value, "raw": item.raw}
                                                             for cid, item in settings.items()}}, indent=2))
        return 0
    except (SolisError, ValueError, OSError) as exc:
        print(f"Read failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
