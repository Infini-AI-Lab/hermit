from hermit.agent.evolver.methods.she.runtime import validate_bundle


def check(adapter_path):
    try:
        validate_bundle(adapter_path)
    except Exception as exc:
        return [{"path": "artifacts", "problem": f"{type(exc).__name__}: {exc}"}]
    return []
