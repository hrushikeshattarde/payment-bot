"""
Fetch a single CargoTel load-maintenance page and dump the raw HTML to a file.

Same auth path as hit_cargo_tel.py: the cgt-browser-session cookie is pulled from
S3 (circle-bot-cookies/rubicon/cargotel.json), refreshed by the login bot.

    python pull_load_detail.py 123456
    python pull_load_detail.py 123456 --out C:\\tmp\\load_123456.html
    python pull_load_detail.py 123456 123457 123458 --out-dir loads/
"""

import argparse
import json
import os
import sys

import boto3
import requests

BASE_URL = "https://circle.cargotel.com/backoffice/loadmaint.mcgi"


def fetch_json_from_s3():
    """
    Fetch the CargoTel session-cookie JSON from S3.

    :return: Python list/dict with the cookie data, or None on failure
    """
    s3 = boto3.client('s3')

    try:
        response = s3.get_object(Bucket="circle-bot-cookies", Key="rubicon/cargotel.json")

        file_content = response['Body'].read().decode('utf-8')

        json_data = json.loads(file_content)

        return json_data

    except Exception as e:
        print(f"Error fetching JSON from S3: {e}")
        return None


def get_load_detail_html(load_id, cookie_json) -> str | None:
    """
    GET /backoffice/loadmaint.mcgi?load_id=<load_id> and return the raw HTML.

    :param load_id: CargoTel load id
    :param cookie_json: cookie JSON as returned by fetch_json_from_s3()
    :return: HTML string, or None if the request failed
    """
    cookie = cookie_json[0]['value']

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "en-US,en;q=0.9",
        "cookie": f"cgt-browser-session={cookie}",
        "priority": "u=0, i",
        "referer": "https://circle.cargotel.com/exe/menuframe.mcgi",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    }

    response = requests.get(BASE_URL, headers=headers, params={"load_id": load_id})

    if response.status_code != 200:
        print(f"Cargotel request errored for load {load_id}: HTTP {response.status_code}")
        return None

    # requests handles the gzip/deflate/br decoding; .text applies the response encoding.
    html = response.text

    # A stale cookie still returns 200 with the login page, so flag the obvious case
    # instead of silently writing a useless file.
    if "loadmaint" not in html.lower() and "login" in html.lower():
        print(
            f"Load {load_id}: response looks like a login page — the S3 session cookie "
            "is probably expired."
        )

    return html


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Dump CargoTel loadmaint.mcgi HTML for one or more load ids."
    )
    parser.add_argument("load_ids", nargs="+", help="CargoTel load id(s)")
    parser.add_argument(
        "--out",
        help="Output file (single load id only). Default: load_<id>.html in --out-dir.",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="Directory for output files (default: current directory)",
    )
    args = parser.parse_args(argv)

    if args.out and len(args.load_ids) > 1:
        parser.error("--out only works with a single load id; use --out-dir instead.")

    cookie_json = fetch_json_from_s3()
    if not cookie_json:
        print("No cookie available — aborting.")
        return 1

    os.makedirs(args.out_dir, exist_ok=True)

    failures = 0
    for load_id in args.load_ids:
        html = get_load_detail_html(load_id, cookie_json)

        if html is None:
            failures += 1
            continue

        out_path = args.out or os.path.join(args.out_dir, f"load_{load_id}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"Load {load_id}: wrote {len(html)} chars to {out_path}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
