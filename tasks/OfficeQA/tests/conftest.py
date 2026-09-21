"""Keep the task's independent host modules off the framework's import surface."""
from pathlib import Path
import sys

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / 'host'))
sys.path.insert(0, str(TASK.parents[1]))
