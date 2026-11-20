"""Prepare a private Gitea repository and isolated working clones for AHE."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GiteaError(RuntimeError):
    """A Gitea API or repository preparation failure."""


def _api_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/api/v1/" + path.lstrip("/")


def _request(base_url: str, token: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        _api_url(base_url, path),
        data=data,
        method=method,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as exc:
        if exc.code == 404:
            return 404, None
        detail = exc.read().decode(errors="replace")[:400]
        raise GiteaError(f"Gitea API {method} {path} failed with HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise GiteaError(f"Gitea API {method} {path} could not be reached") from exc


def ensure_repository(*, base_url: str, owner: str, name: str, token: str) -> dict:
    """Create a private repository when absent and return its API record."""
    path = f"repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    status, record = _request(base_url, token, "GET", path)
    if status == 200:
        return record
    if status != 404:
        raise GiteaError(f"unexpected Gitea response while reading {owner}/{name}: {status}")

    user_status, user = _request(base_url, token, "GET", "user")
    if user_status != 200:
        raise GiteaError("could not identify the Gitea API user")
    if user.get("login") == owner:
        create_path = "user/repos"
    else:
        create_path = f"orgs/{quote(owner, safe='')}/repos"
    status, record = _request(
        base_url,
        token,
        "POST",
        create_path,
        {"name": name, "private": True, "auto_init": False},
    )
    if status not in (200, 201):
        raise GiteaError(f"could not create private repository {owner}/{name}")
    return record


def _git(repo: Path, *args: str, env=None, check=True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _with_askpass(token: str, owner: str):
    directory = Path(tempfile.mkdtemp(prefix="ahe-git-credentials-"))
    script = directory / "askpass.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"case \"$1\" in *Username*) printf '%s\\n' {shlex.quote(owner)};; *) printf '%s\\n' {shlex.quote(token)};; esac\n"
    )
    script.chmod(0o700)
    env = {
        **__import__("os").environ,
        "GIT_ASKPASS": str(script),
        "GIT_TERMINAL_PROMPT": "0",
    }
    return directory, env


def clone_repository(*, clone_url: str, destination: Path, token: str, owner: str) -> Path:
    """Clone a repository with a temporary askpass helper, never storing a token."""
    destination = Path(destination).resolve()
    if destination.exists():
        if not (destination / ".git").exists():
            raise GiteaError(f"clone destination is not a Git repository: {destination}")
        if _git(destination, "status", "--porcelain"):
            raise GiteaError(f"clone destination has local changes: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    askpass, env = _with_askpass(token, owner)
    try:
        subprocess.run(
            ["git", "clone", clone_url, str(destination)],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GiteaError(f"could not clone {clone_url}") from exc
    finally:
        shutil.rmtree(askpass, ignore_errors=True)
    return destination


def seed_repository(*, template: Path, repository: dict, destination: Path, token: str, owner: str) -> Path:
    """Push the adapter template to an empty Gitea repository, then clone it."""
    clone_url = repository.get("clone_url") or repository.get("html_url")
    if not clone_url:
        raise GiteaError("Gitea repository response has no clone URL")
    destination = Path(destination).resolve()
    if repository.get("empty"):
        with tempfile.TemporaryDirectory(prefix="ahe-gitea-seed-") as tmp:
            seed = Path(tmp) / "seed"
            shutil.copytree(template, seed)
            subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
            _git(seed, "config", "user.name", "AHE-Beta")
            _git(seed, "config", "user.email", "ahe-beta@example.invalid")
            _git(seed, "add", ".")
            _git(seed, "commit", "-m", "chore: initialize AHE adapter")
            askpass, env = _with_askpass(token, owner)
            try:
                subprocess.run(
                    ["git", "-C", str(seed), "push", clone_url, "HEAD:main"],
                    check=True,
                    env=env,
                    capture_output=True,
                    text=True,
                )
            finally:
                shutil.rmtree(askpass, ignore_errors=True)
    return clone_repository(clone_url=clone_url, destination=destination, token=token, owner=owner)
