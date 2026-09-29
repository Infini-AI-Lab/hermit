"""submit — hand an answer to the harness commit gate and print what it says.

Usage:
    submit --answer "12.34" [--alt "0.1234"] [--reading "fiscal year"] [--keep "<reason>"] [--artifact PATH]
POSTs a JSON object with the fields contracts.SUBMIT_FIELDS to the URL in $HARNESS_SUBMIT_URL
(contracts.SUBMIT_ENV), which must be http://127.0.0.1:<port>/v1/submit; anything else is refused.
Prints the gate's message (the response's JSON 'message', or the plain-text body). Exit codes:
0 = the gate answered, 1 = the gate rejected the request, 2 = usage/environment error. Stdlib only;
no proxies, no redirects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    from harness_core.contracts import SUBMIT_ENV, SUBMIT_PATH
except ImportError:                      # installed as a standalone script in the kit
    SUBMIT_ENV, SUBMIT_PATH = 'HARNESS_SUBMIT_URL', '/v1/submit'

MAX_ARTIFACT = 256 << 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def check_url(url: str) -> str | None:
    """Why `url` is not an acceptable submit route, or None."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return 'malformed URL'
    if parts.scheme != 'http' or parts.hostname != '127.0.0.1' or port is None:
        return 'only http://127.0.0.1:<port> is allowed'
    if parts.username or parts.password or parts.query or parts.fragment or parts.path != SUBMIT_PATH:
        return f'the route must be exactly {SUBMIT_PATH}'
    return None


def artifact_sha(path: str) -> str:
    # Check before opening: open() on a FIFO would block forever.
    if not os.path.isfile(path):
        raise ValueError('artifact must be a regular file ≤ 256 MB')
    with open(path, 'rb') as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_ARTIFACT:
            raise ValueError('artifact must be a regular file ≤ 256 MB')
        h = hashlib.sha256()
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def message_of(body: bytes) -> str:
    text = body.decode('utf-8', 'replace')
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()
    if isinstance(data, dict):
        for key in ('message', 'error'):
            if isinstance(data.get(key), str):
                return data[key]
    return text.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='submit', description='Submit an answer to the harness gate.')
    ap.add_argument('--answer', help='the answer, exactly as it should be graded')
    ap.add_argument('--alt', help='another answer you considered')
    ap.add_argument('--reading', help='the reading/assumption the answer rests on')
    ap.add_argument('--keep', help='keep the answer against the last finding, with a reason')
    ap.add_argument('--artifact', help='path of an answer file (artifact tasks)')
    args = ap.parse_args(argv)
    url = os.environ.get(SUBMIT_ENV, '')
    if not url:
        print(f'submit: no submit route in this environment ({SUBMIT_ENV} is not set)', file=sys.stderr)
        return 2
    problem = check_url(url)
    if problem:
        print(f'submit: refusing {SUBMIT_ENV}: {problem}', file=sys.stderr)
        return 2
    payload = {k: getattr(args, k) for k in ('answer', 'alt', 'reading', 'keep') if getattr(args, k) is not None}
    if args.artifact:
        try:
            payload['artifact_sha256'] = artifact_sha(args.artifact)
        except (OSError, ValueError) as exc:
            print(f'submit: cannot read artifact: {exc}', file=sys.stderr)
            return 2
    if not (payload.get('answer') or '').strip() and 'artifact_sha256' not in payload:
        print('submit: give --answer "<answer>" (or --artifact PATH)', file=sys.stderr)
        return 2
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=180) as response:
            print(message_of(response.read()))
            return 0
    except urllib.error.HTTPError as exc:
        print(message_of(exc.read()) or f'submit: gate error HTTP {exc.code}', file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f'submit: cannot reach the harness gate ({getattr(exc, "reason", exc)})', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
