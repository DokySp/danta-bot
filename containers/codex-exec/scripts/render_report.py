#!/usr/bin/env python3
"""Render the authoritative README without network assets or raw HTML."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from danta.reporting import render_readme


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'README.md')
    parser.add_argument('--output', type=Path, default=ROOT / 'report.html')
    args = parser.parse_args()
    print(json.dumps(render_readme(args.source, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
