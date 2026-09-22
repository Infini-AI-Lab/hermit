"""Explicit, frozen experiment profiles. Legacy reproductions keep their defaults."""
from paths import _config

PROFILE = _config.get('profile', 'memory_bounded')
if PROFILE not in ('memory_bounded', 'full_mwm_unbounded'):
    raise ValueError('Unknown OfficeQA experiment profile')
FULL = PROFILE == 'full_mwm_unbounded'
ACTOR_SECONDS = None if FULL else 1800
BROKER_OPTIONS = ({'max_completion': None, 'max_response': None, 'request_timeout': None,
                   'max_attempts': None} if FULL else {})
EXECUTION_IMAGE = _config.get('execution_image', 'officeqa-mwm-exec:20260921')
