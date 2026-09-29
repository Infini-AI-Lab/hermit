"""pytest configuration for the v9 plumbing tests: marker registration and the two opt-in groups.

- `slow`: tests that take over ~60 s or sweep the full tables index / recorded v8 traffic. Opt in with a
  `-m` expression naming it (`-m slow`, `-m "slow or live_codex"`) or V9_SLOW=1 (V9_SLOW=all additionally
  widens tests/test_v10_ledger.py's v8 sweep from 4 sample runs to all 399).
- `live_codex`: tests that launch the pinned real Codex binary against a fake upstream on 127.0.0.1 ($0, no
  credentials). Opt in with `-m live_codex` or V9_LIVE_CODEX=1.

Without an opt-in, tests carrying these markers are collected and reported as skipped, so a plain
`pytest tests` stays fast and offline.
"""
import os

import pytest

OPT_IN = {
    'slow': ('V9_SLOW', 'long-running (> 60 s) or full-corpus / full-v8 sweeps'),
    'live_codex': ('V9_LIVE_CODEX', 'launches the pinned real Codex binary against a local fake upstream ($0)'),
}


def pytest_configure(config):
    for name, (env, what) in OPT_IN.items():
        config.addinivalue_line('markers', f'{name}: {what}; opt-in with -m {name} or {env}=1')


def opted_in(config, name: str) -> bool:
    expression = config.getoption('-m') or ''
    return name in expression or bool(os.environ.get(OPT_IN[name][0]))


def pytest_collection_modifyitems(config, items):
    for name, (env, _) in OPT_IN.items():
        if opted_in(config, name):
            continue
        skip = pytest.mark.skip(reason=f'{name} tests are opt-in: -m {name} or {env}=1')
        for item in items:
            if item.get_closest_marker(name) is not None:
                item.add_marker(skip)
