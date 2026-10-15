# Deploy

Finished adapters, ready to check against their template and to install into an agent's workspace with
`harnessadapter deploy`.

| Folder | Adapter |
|---|---|
| [`lean_collab/`](lean_collab/) | The Lean collaboration adapter v12.5: five Codex agents proving one Lean theorem together |

```bash
python -m harnessadapter.spec examples/deploy/lean_collab/adapter --root harnessadapter/templates/lean_collab
harnessadapter deploy --adapter examples/deploy/lean_collab/adapter --workspace /tmp/agent-A1 --host codex
```
