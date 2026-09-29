"""Explicit local paths; runtime copies never depend on a parent project layout."""
import json
import os
from pathlib import Path
import shutil

SOURCE = Path(__file__).resolve().parent
_config_path = SOURCE / '.task-paths.json'
_config = json.loads(_config_path.read_text()) if _config_path.is_file() else {}
REPOSITORY = Path(_config.get('framework', SOURCE.parents[2])).resolve()
TASK = REPOSITORY / 'tasks/OfficeQA'
VENDOR = SOURCE / 'vendor/officeqa' if _config else TASK / 'vendor/officeqa'
DEPLOYMENT = SOURCE / 'deployment' if _config else TASK / 'deployments/officeqa'
DATA_CACHE = Path(_config.get('data_cache', TASK / '.data/officeqa-official')).resolve()
_bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
_codex = (_config.get('codex') or os.environ.get('OFFICEQA_CODEX') or
          (str(_bundled) if _bundled.is_file() else shutil.which('codex')))
CODEX = Path(_codex or '/Applications/ChatGPT.app/Contents/Resources/codex').absolute()
