"""Install framework dependencies once, outside every adapter core."""
from pathlib import Path
import subprocess
import sys
import venv


def ensure_environment(root: Path) -> Path:
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or newer is required")
    environment = root / ".venv-demo"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        print("First use: creating an isolated framework environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    probe = subprocess.run([str(python), "-c",
                            "from importlib.metadata import version; from mcp.server import MCPServer; "
                            "import jsonschema, yaml; assert version('mcp') == '2.2.0' and version('jsonschema') == '4.26.0' and version('PyYAML') == '6.0.3'"], capture_output=True)
    if probe.returncode:
        print("Installing framework dependencies...", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(root / "framework/requirements.txt")], check=True)
    return python
