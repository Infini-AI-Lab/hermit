"""Small authorized connection check; credential is read without terminal echo."""
import getpass
import json
import os
from pathlib import Path
import time
import urllib.request
import urllib.error

root = Path(__file__).resolve().parent
key = os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
payload = {'model': 'accounts/fireworks/models/glm-5p3-flash',
           'input': 'Reply with OK only.', 'max_output_tokens': 256,
           'temperature': 0, 'store': False, 'stream': False}
request = urllib.request.Request('https://api.fireworks.ai/inference/v1/responses',
    data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + key,
    'Content-Type': 'application/json'}, method='POST')
start = time.monotonic()
try:
    with urllib.request.urlopen(request, timeout=45) as response:
        result = {'http_status': response.status, 'response': json.load(response)}
except urllib.error.HTTPError as exc:
    result = {'http_status': exc.code, 'error': exc.read().decode(errors='replace').replace(key, '[REDACTED]')[:1500]}
except Exception as exc:
    result = {'http_status': None, 'error': str(exc).replace(key, '[REDACTED]')[:1000]}
result['seconds'] = round(time.monotonic() - start, 3)
destination = root / 'preflight/api-responses.json'
if destination.exists():
    raise ValueError('Do not overwrite original preflight result')
destination.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
