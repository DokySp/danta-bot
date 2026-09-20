"""Run the same local-only isolation probe used by deployed startup."""
import argparse
import json
from pathlib import Path
from danta.adapters.isolation_probe import probe

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--write-result", type=Path)
    parser.add_argument("--model-id", default="gpt-5.6-sol")
    args = parser.parse_args()
    result = probe(args.model_id)
    if args.write_result:
        args.write_result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
