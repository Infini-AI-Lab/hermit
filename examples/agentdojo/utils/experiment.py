"""Run the shared frozen train/none/root/final experiment supervisor for AgentDojo."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agentdyn/utils"))
from run_experiment import main


if __name__ == '__main__':
    main()
