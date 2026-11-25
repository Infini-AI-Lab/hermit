"""Prepare a private Gitea repository and isolated working clones for AHE."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class GiteaError(RuntimeError):
    """A Gitea API or repository preparation failure."""


def loopback(host: str | None) -> bool:
    """Whether a host name or address is this machine's own (any of 127/8, ::1, an IPv4 one mapped, `localhost`): a
    sandbox's proxy never lists one, which would open every port there."""
    host = (host or "").rstrip(".").lower()             # `localhost.` is localhost
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:                                             # 127.1, 0x7f.1, 2130706433: what the C library reads
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or address.is_unspecified or bool(mapped and mapped.is_loopback)


# Git's askpass for a private repository: the user and the token from the environment, never written down, and only
# for the Gitea's own origin (scheme, host and port, as Git's prompt names it, `origin`): any other prompt gets nothing.
ASKPASS = ('#!/bin/sh\ncase "$1" in *"\'$AHE_GIT_ORIGIN\'"*|*"\'$AHE_GIT_ORIGIN_USER\'"*) ;; *) exit 1;; esac\n'
           'case "$1" in *Username*) printf \'%s\\n\' "$AHE_GIT_USER";; *) printf \'%s\\n\' "$AHE_GIT_TOKEN";; esac\n')


def origin(url: str, user: str) -> dict:
    """The Gitea's origin as Git's credential prompts name it (`scheme://host[:port]`, with the user too): ASKPASS's."""
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return {"AHE_GIT_ORIGIN": f"{parts.scheme}://{netloc}", "AHE_GIT_ORIGIN_USER": f"{parts.scheme}://{user}@{netloc}"}


def plain(base_url: str) -> bool:
    """Whether a Gitea URL would carry its token in the clear to another machine (http, not this machine's)."""
    url = urlsplit(base_url)
    return url.scheme == "http" and not loopback(url.hostname)


def git_url(base_url: str, owner: str, repo: str) -> str:
    """The repository's Git URL at the Gitea this run reaches (not the one its API advertises, which may be another
    origin, out of the sandbox's network)."""
    return f"{base_url.rstrip('/')}/{owner}/{repo}.git"


class _SameOrigin(HTTPRedirectHandler):
    """Redirects within Gitea's own origin only: urllib carries the Authorization header wherever one leads."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl)[:2] != urlsplit(req.full_url)[:2]:
            raise GiteaError(f"Gitea redirected to another origin ({urlsplit(newurl).netloc}): its token stays here")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPEN = build_opener(_SameOrigin).open


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
        with _OPEN(request, timeout=30) as response:
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


def _with_askpass(token: str, owner: str, url: str):
    """An askpass helper that reads the user and the token from the environment it is given, for `url`'s host only:
    a helper left behind (an interrupted run) holds no credential."""
    directory = Path(tempfile.mkdtemp(prefix="ahe-git-credentials-"))
    script = directory / "askpass.sh"
    script.write_text(ASKPASS)
    script.chmod(0o700)
    env = {
        **__import__("os").environ,
        "GIT_ASKPASS": str(script),
        "GIT_TERMINAL_PROMPT": "0",
        "AHE_GIT_USER": owner,
        "AHE_GIT_TOKEN": token,
        **origin(url, owner),
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
    askpass, env = _with_askpass(token, owner, clone_url)
    try:
        subprocess.run(                                  # no credential helper of this machine's keeps the token
            ["git", "-c", "credential.helper=", "clone", clone_url, str(destination)],
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
            shutil.copytree(template, seed, ignore=shutil.ignore_patterns("__pycache__"))
            subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
            _git(seed, "config", "user.name", "AHE-Beta")
            _git(seed, "config", "user.email", "ahe-beta@example.invalid")
            _git(seed, "add", "--all", "--force", ".")   # every file, whatever this machine's own ignore rules say
            committed = set(_git(seed, "ls-files", "-z").split("\0")) - {""}
            expected = {p.relative_to(template).as_posix() for p in Path(template).rglob("*")
                        if p.is_file() and "__pycache__" not in p.parts}
            if committed != expected:
                raise GiteaError(f"the seed would not hold the template's files: {sorted(expected ^ committed)}")
            _git(seed, "commit", "-m", "chore: initialize AHE adapter")
            askpass, env = _with_askpass(token, owner, clone_url)
            try:
                subprocess.run(
                    ["git", "-c", "credential.helper=", "-C", str(seed), "push", clone_url, "HEAD:main"],
                    check=True,
                    env=env,
                    capture_output=True,
                    text=True,
                )
            finally:
                shutil.rmtree(askpass, ignore_errors=True)
    return clone_repository(clone_url=clone_url, destination=destination, token=token, owner=owner)
