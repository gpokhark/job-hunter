#!/usr/bin/env python3
"""Read-only helper for inspecting a candidate public endpoint during development."""

import argparse
import json
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("url")
args = parser.parse_args()
request = urllib.request.Request(args.url, headers={"User-Agent": "JobHunter endpoint discovery"})
with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
    body = response.read(200_000)
    print(
        json.dumps(
            {
                "status": response.status,
                "content_type": response.headers.get_content_type(),
                "bytes_sampled": len(body),
                "sample": body[:2000].decode(errors="replace"),
            },
            indent=2,
        )
    )
