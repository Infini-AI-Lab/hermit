"""Explicit, frozen experiment profiles. Legacy reproductions keep their defaults."""
from pathlib import Path

from paths import _config, SOURCE

PROFILE = _config.get('profile', 'memory_bounded')
PROFILES = ('memory_bounded', 'full_mwm_unbounded', 'full_prompt_only_unbounded')
if PROFILE not in PROFILES:
    raise ValueError('Unknown OfficeQA experiment profile')
FULL = PROFILE.startswith('full_')
# One architecture switch consumed by the campaign, actors, learner and audits.
ARCHITECTURE = 'prompt_only' if 'prompt_only' in PROFILE else 'mwm'
MWM = ARCHITECTURE == 'mwm'
# Operator-set per-question wall clock (2026-09-24): identical for both arms, recorded in
# invocation.json; a question that runs out is status 'timeout' with an empty answer.
ACTOR_SECONDS = 3600 if FULL else 1800
BROKER_OPTIONS = ({'max_completion': None, 'max_response': None, 'request_timeout': None,
                   'max_attempts': None} if FULL else {})
EXECUTION_IMAGE = _config.get('execution_image', 'officeqa-mwm-exec:20260921')
# Full profiles: both arms advance one block at a time so they face the same
# provider conditions, and the learning round after Q50 is skipped (no actor
# would consume it). Retries and byte limits are accounting, never truncation.
LOCKSTEP = FULL
LEARN_LAST_BLOCK = False
PROJECT_DOC_MAX_BYTES = 4194304
# Read-only reference material granted to the learner only (never to actors).
SHELF = SOURCE / 'reference-shelf' if (SOURCE / 'reference-shelf').is_dir() else None
