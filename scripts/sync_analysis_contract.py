"""Copy/check the analysis contract from its sole AI-orchestration source."""

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = (
    ROOT.parent / "Bond_Y_agent/src/agent_platform/application/contracts/analysis-execution.v1.json"
)
TARGET = ROOT / "src/sandbox/contracts/analysis-execution.v1.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true", help="Write the contract copy (default)")
    args = parser.parse_args()
    content = args.source.read_text(encoding="utf-8").replace("\r\n", "\n")
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != content:
            raise SystemExit("Analysis contract copy has drifted; run sync_analysis_contract.py")
    else:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
