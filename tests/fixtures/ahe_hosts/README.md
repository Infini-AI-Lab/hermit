# Hook payloads of the hosts' own CLIs

Real stdin payloads of Codex 0.164.0 and Claude Code 2.1.317, each run on a scripted local model with the hook
registered on every event (no network, no model): one file per event and case, the run's directory replaced by
`/probe` and the home by `/home/user`. tests/test_ahe_hosts.py replays them through hook_hosts.py's wires and holds
the template check's synthetic events to their shape. Codex ran gpt-5.6-luna (code mode: the shell is the `exec`
script's `exec_command`).
