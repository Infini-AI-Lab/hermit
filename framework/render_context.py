#!/usr/bin/env python3
"""Render an adapter's context using the framework's installed environment."""
import argparse
import json
from pathlib import Path

from adapter import Adapter

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    args = parser.parse_args()
    adapter = Adapter(args.adapter)
    print(json.dumps({"context": adapter.context(), "agents": adapter.agent_definitions()}))
