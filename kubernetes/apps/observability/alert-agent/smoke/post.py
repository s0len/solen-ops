"""POST one webhook body to Hermes, signed or unsigned. Stdlib only.

Usage: post.py <url> <secret|-> <body-file>
Prints "<status> <response body>"; exits 0 whatever the status (the caller asserts).
"""

import hashlib
import hmac
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid


def main() -> int:
    url, secret, body_path = sys.argv[1], sys.argv[2], sys.argv[3]
    body = pathlib.Path(body_path).read_bytes()
    headers = {"Content-Type": "application/json", "X-Request-ID": str(uuid.uuid4())}
    if secret != "-":
        timestamp = str(int(time.time()))
        signed = timestamp.encode() + b"." + body
        headers["X-Webhook-Timestamp"] = timestamp
        headers["X-Webhook-Signature-V2"] = hmac.new(
            secret.encode(), signed, hashlib.sha256
        ).hexdigest()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.status, response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode("utf-8", "replace"))
    except Exception as exc:  # connection refused, timeout, ...
        print("000", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
