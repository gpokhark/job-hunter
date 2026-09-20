#!/usr/bin/env python3
"""Standalone LM Studio reachability check — no archive, no resume, no review, just: is the
configured LM Studio server actually answering right now?

Exists because `job-reviewer`'s `model_unavailable` status only ever surfaces after a full
review attempt, and an agent runtime can misreport "LM Studio is down" when the real problem is
its own network path to the configured `base_url` (a different container/host than the one
LM Studio is running on, a stale LAN IP, a firewall) rather than the model server itself —
confirmed live with Hermes. Run this first whenever a review step claims the server is
unreachable, before assuming the server itself is actually down: it makes the exact same
`GET {base_url}/models` call `job-reviewer` does, in isolation, with no other setup required.

Usage:
    uv run python scripts/check_lm_studio.py
    uv run python scripts/check_lm_studio.py --project /path/to/job-hunter

Exit code 0 = reachable, 1 = not reachable. Prints one line either way — no JSON, nothing to
parse — since this is meant to be run and read directly, by a human or an agent deciding what
to try next.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_hunter.lm_studio_health import check_lm_studio, load_lm_studio_config
from job_hunter.rootutil import add_project_argument, chdir_to_project_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/lm_studio.yaml"), help="LM Studio connection config"
    )
    add_project_argument(parser)
    args = parser.parse_args(argv)
    chdir_to_project_root(args.project)

    try:
        config = load_lm_studio_config(args.config)
    except FileNotFoundError as exc:
        print(f"FAIL LM Studio: {exc}", file=sys.stderr)
        return 1

    reachable, detail = check_lm_studio(config)
    print(f"{'OK' if reachable else 'FAIL'} LM Studio: {detail}")
    return 0 if reachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
