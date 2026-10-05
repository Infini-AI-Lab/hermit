# Example: OfficeQA

Questions about U.S. Treasury Bulletins, answered by a GLM 5.3 Flash tool-calling agent (`pipeline.py`) and scored
by the official strict scorer (`judge.py`).

```bash
python prepare.py /path/to/officeqa_official_133/prepared     # tasks.jsonl, splits/, data_root.txt
export FIREWORKS_API_KEY=...
harnessadapter check --config smoke.yaml
harnessadapter train --config smoke.yaml
harnessadapter eval  --config smoke.yaml --adapter final
harnessadapter eval  --config smoke.yaml --adapter none        # the agent without an adapter
```

The prepared directory holds the corpus (agent/) and the answers and scorer (private/). The judge reads the private
part. A sandboxed Evolver sees only its workspace, its accesses and `sandbox.read` (empty here), so neither the
answers nor the example directory are visible to it.
