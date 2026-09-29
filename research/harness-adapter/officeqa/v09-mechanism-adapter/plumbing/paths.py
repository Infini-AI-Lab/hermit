"""Explicit local paths; runtime copies never depend on a parent project layout.

v9 additions (L0 kit): the frozen actor runtime (a hashed copy of the codex runtime python and of
ChatGPT.app's rg under `frameworks-v9/`), the benchmark-neutral `harness_core/` and the `packs/`
code roots the kit wrappers execute, and a pinned Codex default. Every path can be overridden from
`.task-paths.json` so a run root records exactly what its actors used.
"""
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
# v9: the pinned 0.155.0-alpha.9.2 binary is the default; the app-bundled codex changes with app updates.
_pinned = SOURCE.parent / 'frameworks-v8/codex-0.155.0-alpha.9.2/codex'
_bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
_codex = (_config.get('codex') or os.environ.get('OFFICEQA_CODEX') or
          (str(_pinned) if _pinned.is_file() else str(_bundled) if _bundled.is_file() else shutil.which('codex')))
CODEX = Path(_codex or '/Applications/ChatGPT.app/Contents/Resources/codex').absolute()

# v9 L0: frozen runtime copies (read-only, hashed by manifest; see kit.py and frameworks-v9/).
FROZEN = Path(_config.get('frozen_runtime', SOURCE.parent / 'frameworks-v9')).absolute()
ACTOR_PYTHON_PREFIX = Path(_config.get('actor_python_prefix', FROZEN / 'actor-python')).absolute()
ACTOR_PYTHON = ACTOR_PYTHON_PREFIX / 'bin/python3.12'
ACTOR_PYTHON_MANIFEST = ACTOR_PYTHON_PREFIX / 'manifest.json'
ACTOR_RG = Path(_config.get('actor_rg', FROZEN / 'bin/rg')).absolute()
ACTOR_RG_SHA256_FILE = ACTOR_RG.with_name(ACTOR_RG.name + '.sha256')
# Code the kit wrappers execute; granted read-only to the actor (never the adapter or run records).
HARNESS_CORE = SOURCE / 'harness_core'
PACKS = SOURCE / 'packs'
