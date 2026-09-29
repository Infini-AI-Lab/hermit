"""Frozen v9 experiment constants (DESIGN §1, §2, §4). One arm per root, parsed once by arms.py.

The arm comes only from `.task-paths.json` "arm" (a standard name or a full spec) via
`arms.parse_config`; nothing here infers an architecture from a profile or directory name. A directory
without a `.task-paths.json` (the plumbing source itself, tests) has CAMPAIGN = ARM = None.

Names kept for acting.py (FULL, MWM, PROJECT_DOC_MAX_BYTES) and for the v8 legacy modules that still sit
in the tree (ARCHITECTURE, SHELF, EXECUTION_IMAGE); none of the legacy learners is used by a v9 arm.
"""
from paths import _config, SOURCE
import arms

PROFILE = 'v9'
if _config and _config.get('profile', PROFILE) != PROFILE:
    raise ValueError(f"v9 plumbing needs profile 'v9' in .task-paths.json (got {_config.get('profile')!r})")
# The campaign spec of this root: arm (contracts.ArmSpec), repeat, pack, order seed, barrier.
CAMPAIGN = arms.parse_config(_config, root_name=SOURCE.name) if 'arm' in _config else None
ARM = CAMPAIGN.arm if CAMPAIGN else None
# 'none' | 'text' | 'mechanism': an explicit function of ARM.seed (arms.architecture); 'none' without a campaign.
ARCHITECTURE = arms.architecture(ARM) if ARM else 'none'
# v8 name: the unbounded transport profile (v8 FULL). Every v9 arm uses it.
FULL = True
# No v9 arm serves the MWM runtime (acting.py only consults it for adapters with an MCP server).
MWM = False
# Per-question wall clock, identical for every arm (v8 FULL value); recorded in invocation.json.
ACTOR_SECONDS = 3600
# v8 FULL broker options: no completion, response, timeout or attempt caps; retries are accounted.
BROKER_OPTIONS = {'max_completion': None, 'max_response': None, 'request_timeout': None, 'max_attempts': None}
# Learning after every 5 sealed episodes; nothing is learned after the last block (it would serve no actor).
LEARN_EVERY = 5
LEARN_LAST_BLOCK = False
PROJECT_DOC_MAX_BYTES = 4194304
# Decided for all v9 arms (wave-2): the enforcement re-query happens only when the gate has findings.
REQUERY_WHEN_CLEAN = False
# [user decision pending: pilot] actor temperature: this root's declared `.task-paths.json` "temperature"
# (default arms.DEFAULT_TEMPERATURE = 0, the v8 value). The campaign passes CAMPAIGN.temperature to the
# broker (option `temperature`, sent on every upstream call); PREREG.md records the confirmatory value.
TEMPERATURE = CAMPAIGN.temperature if CAMPAIGN else arms.DEFAULT_TEMPERATURE
# wave 3: the root's declared `.task-paths.json` "top_p" (default arms.DEFAULT_TOP_P = None: no top_p is sent,
# the v8 behaviour). Actor episodes only; the learner model calls (client.py) keep their own settings.
TOP_P = CAMPAIGN.top_p if CAMPAIGN else arms.DEFAULT_TOP_P
# Legacy names (v8 modules only; unused by v9 arms).
SHELF = None
EXECUTION_IMAGE = _config.get('execution_image', 'officeqa-mwm-exec:20260921')
