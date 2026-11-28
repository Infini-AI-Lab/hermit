"""Prepare a private Gitea repository and isolated working clones for AHE."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


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
# for the Gitea's own origin (scheme, host and port, as Git's prompt names it, `origin`, with whatever user Git's own
# configuration gives it, credential.username say, and any path after it under credential.useHttpPath: the token is
# the Gitea's, for all of it): any other prompt gets nothing.
ASKPASS = r"""#!/bin/sh
url=${1#*"'"}; url=${url%%"'"*}
rest=${url#*://}; authority=${rest%%/*}
case "${url%%://*}://${authority##*@}${rest#"$authority"}" in
  "$AHE_GIT_ORIGIN"|"$AHE_GIT_ORIGIN"/*) ;;
  *) exit 1;;
esac
case "$1" in *Username*) printf '%s\n' "$AHE_GIT_USER";; *) printf '%s\n' "$AHE_GIT_TOKEN";; esac
"""


def origin(url: str) -> dict:
    """The Gitea's origin as Git's credential prompts name it (`scheme://host[:port]`, its user apart): ASKPASS's."""
    parts = urlsplit(url)
    return {"AHE_GIT_ORIGIN": f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"}


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
_OPEN_DIRECT = build_opener(ProxyHandler({}), _SameOrigin).open   # a Gitea on this machine: never a proxy's


def direct(env: dict, url: str) -> dict:
    """`env` with `url`'s host in NO_PROXY when it is this machine's own: Git (libcurl) and urllib send a proxy every
    host NO_PROXY does not name, a loopback one too, with the token in clear (and a proxy cannot reach it anyway)."""
    host = urlsplit(url).hostname
    if not loopback(host):
        return env
    present = str(env["no_proxy"] if "no_proxy" in env else env.get("NO_PROXY") or "")   # the one read (no_proxy
    names = [n.strip() for n in present.split(",") if n.strip()]                         # wins), "*" kept as it is
    merged = present.strip() if present.strip() == "*" else ",".join(
        names + [h for h in (host, "127.0.0.1", "localhost") if h not in names])
    return {**env, "NO_PROXY": merged, "no_proxy": merged}


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
        with (_OPEN_DIRECT if loopback(urlsplit(base_url).hostname) else _OPEN)(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as exc:
        if exc.code == 404:
            return 404, None
        detail = exc.read().decode(errors="replace")[:400]
        raise GiteaError(f"Gitea API {method} {path} failed with HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise GiteaError(f"Gitea API {method} {path} could not be reached") from exc


def ensure_repository(*, base_url: str, owner: str, name: str, token: str, existing_ok: bool = False,
                      creating=None, marker: str = "") -> dict:
    """Create a private repository and return its API record; an existing one only when `existing_ok` (the cohort's
    own bootstrap made it: another cohort's open issues would be this one's, AHE-Beta's triage taking every open
    issue) or when its description is `marker` (one this cohort made, the answer to its creation lost), and private.
    `creating` is called just before the repository is made."""
    path = f"repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    status, record = _request(base_url, token, "GET", path)
    if status == 200:
        if not existing_ok and not (marker and record.get("description") == marker):
            raise GiteaError(f"{owner}/{name} exists: each cohort takes a repository of its own (AHE-Beta's triage "
                             "takes every open issue in it, an earlier cohort's too); name another")
        if record.get("private") is not True:            # the users' issues, persona traits in their footers
            raise GiteaError(f"{owner}/{name} is public: it would publish the users' issues")
        return record
    if status != 404:
        raise GiteaError(f"unexpected Gitea response while reading {owner}/{name}: {status}")
    if creating is not None:
        creating()

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
        {"name": name, "private": True, "auto_init": False, **({"description": marker} if marker else {})},
    )
    if status not in (200, 201):
        raise GiteaError(f"could not create private repository {owner}/{name}")
    if record.get("private") is not True:                # the users' issues would be public
        raise GiteaError(f"{owner}/{name} was created public: make it private, or name another")
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
        **origin(url),
    }
    return directory, direct(env, url)


def clone_repository(*, clone_url: str, destination: Path, token: str, owner: str, existing_ok: bool = False) -> Path:
    """Clone a repository with a temporary askpass helper, never storing a token; an existing clone only when
    `existing_ok` (the cohort's own bootstrap made it and no team has had it: Git run here in one a team wrote could
    run its configuration on this machine)."""
    destination = Path(destination).resolve()
    if destination.exists():
        if not existing_ok:
            raise GiteaError(f"{destination} exists: bootstrap clones into a folder of its own (Git run on this "
                             "machine in a clone a team wrote could run that team's configuration)")
        if not (destination / ".git").exists():
            raise GiteaError(f"clone destination is not a Git repository: {destination}")
        if not same_repository(_git(destination, "remote", "get-url", "origin", check=False), clone_url):
            raise GiteaError(f"clone destination {destination} is not a clone of {clone_url}")
        if _git(destination, "status", "--porcelain"):
            raise GiteaError(f"clone destination has local changes: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Made in a folder of this call's own beside it, then renamed whole: a stop leaves no half clone where a retry
    # would take it for a whole one, and nothing of anyone else's is removed.
    scratch = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".cloning", dir=destination.parent))
    askpass, env = _with_askpass(token, owner, clone_url)
    try:
        subprocess.run(                                  # no credential helper of this machine's keeps the token
            ["git", "-c", "credential.helper=", "clone", "-c", "core.autocrlf=false",   # the commit's bytes, whatever
             clone_url, str(scratch / "clone")],                                        # this machine says
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        os.replace(scratch / "clone", destination)
    except subprocess.CalledProcessError as exc:
        raise GiteaError(f"could not clone {clone_url}") from exc
    finally:
        shutil.rmtree(askpass, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
    return destination


def same_repository(one: str, other: str) -> bool:
    """Whether two Git URLs name one repository: their scheme, host and port, and their path without a trailing .git
    or slash (a user in either aside)."""
    def key(url):
        parts = urlsplit(url.strip())
        try:
            port = parts.port or {"https": 443, "http": 80, "ssh": 22}.get(parts.scheme)
        except ValueError:
            port = "?"
        path = parts.path.rstrip("/")
        return parts.scheme, (parts.hostname or "").lower(), port, (path[:-4] if path.endswith(".git") else path)
    return bool(one) and key(one) == key(other)


def seed_repository(*, template: Path, repository: dict, destination: Path, token: str, owner: str,
                    existing_ok: bool = False) -> Path:
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
            _git(seed, "config", "core.autocrlf", "false")   # the template's bytes, as Hermit's tree keeps them
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
    return clone_repository(clone_url=clone_url, destination=destination, token=token, owner=owner,
                            existing_ok=existing_ok)
