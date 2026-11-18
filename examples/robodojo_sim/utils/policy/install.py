"""Install the harness's XPolicyLab policy into a RoboDojo checkout (launch.sh runs it):

    python install.py --root $ROBODOJO_ROOT        # -> $ROBODOJO_ROOT/XPolicyLab/policy/hermit_program
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import dojo  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True, help="the RoboDojo checkout")
    print(dojo.install(ap.parse_args().root.resolve()))
