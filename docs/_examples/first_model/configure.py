"""Write endpoint/model names into this copy's run.yaml; never persist the API key."""
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def main() -> None:
    endpoint = (os.environ.get("LEARNER_BASE_URL") or os.environ.get("MODEL_BASE_URL", "")).rstrip("/")
    model = os.environ.get("MODEL_NAME", "")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not model:
        raise SystemExit("Set MODEL_BASE_URL to an HTTP(S) endpoint prefix and MODEL_NAME to a model id.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("Use an endpoint prefix without credentials, query parameters, or fragments.")
    key_env = "LEARNER_API_KEY" if os.environ.get("LEARNER_API_KEY") else "MODEL_API_KEY"
    if not os.environ.get(key_env):
        raise SystemExit("Set MODEL_API_KEY or LEARNER_API_KEY; it will be read at runtime and will not be saved.")
    path = Path(__file__).with_name("run.yaml")
    config = yaml.safe_load(path.read_text())
    config["llm"]["base_url"] = endpoint
    config["llm"]["model"] = os.environ.get("LEARNER_MODEL") or model
    config["llm"]["api_key_env"] = key_env
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    print("Configured learner endpoint/model in run.yaml; API credential remains in the environment.")


if __name__ == "__main__":
    main()
