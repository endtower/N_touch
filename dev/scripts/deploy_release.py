"""Deploy the source layout and migrate local data with verified backups."""
import argparse
from pathlib import Path
from deploy_source_layout import deploy, ROOT
from workspace_layout import BUILDS

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program-source", type=Path, default=BUILDS / "independent-updates/app")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    deploy(args.program_source, args.execute)
