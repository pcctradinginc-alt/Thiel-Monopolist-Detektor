"""
Deploys the Thiel Feedback Worker to Cloudflare via REST API.
Reads CF_TOKEN, GH_LABEL_TOKEN, WEBHOOK_SECRET from environment.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path


def cf_request(method, path, data=None, form_data=None):
    token = os.environ["CF_TOKEN"]
    url = f"https://api.cloudflare.com/client/v4{path}"
    headers = {"Authorization": f"Bearer {token}"}

    if form_data:
        boundary = "----FormBoundary7MA4YWxkTrZu0gW"
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        body_parts = []
        for name, (content, ctype) in form_data.items():
            body_parts.append(f"--{boundary}".encode())
            body_parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
            body_parts.append(f"Content-Type: {ctype}".encode())
            body_parts.append(b"")
            body_parts.append(content if isinstance(content, bytes) else content.encode())
        body_parts.append(f"--{boundary}--".encode())
        body = b"\r\n".join(body_parts)
    elif data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    else:
        body = None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def main():
    token = os.environ.get("CF_TOKEN", "")
    gh_token = os.environ.get("GH_LABEL_TOKEN", "")
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")

    if not token:
        print("ERROR: CF_TOKEN not set")
        sys.exit(1)

    # Step 1: Get Account ID
    resp = cf_request("GET", "/accounts?per_page=1")
    if not resp.get("success") or not resp.get("result"):
        print(f"ERROR getting account: {resp.get('errors')}")
        sys.exit(1)
    account_id = resp["result"][0]["id"]
    print(f"Account ID: {account_id}")

    # Step 2: Upload worker script
    script = Path(__file__).parent.joinpath("worker.js").read_text()
    metadata = json.dumps({"main_module": "worker.js"})

    # Build multipart body manually with correct filename in Content-Disposition
    boundary = "----CloudflareBoundary7MA4YWxkTrZu0gW"
    crlf = b"\r\n"

    def part(name, content, ctype, filename=None):
        disp = f'form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        lines = [
            f"--{boundary}".encode(),
            f"Content-Disposition: {disp}".encode(),
            f"Content-Type: {ctype}".encode(),
            b"",
            content if isinstance(content, bytes) else content.encode("utf-8"),
        ]
        return crlf.join(lines)

    body = crlf.join([
        part("metadata", metadata, "application/json"),
        part("worker.js", script, "application/javascript+module", filename="worker.js"),
        f"--{boundary}--".encode(),
    ])

    token = os.environ["CF_TOKEN"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/thiel-feedback"
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
    )
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        resp = json.loads(e.read())

    if resp.get("success"):
        print("Worker script uploaded OK")
    else:
        print(f"ERROR uploading script: {resp.get('errors')}")
        sys.exit(1)

    # Step 3: Set secrets
    for name, value in [("GH_LABEL_TOKEN", gh_token), ("WEBHOOK_SECRET", webhook_secret)]:
        if not value:
            print(f"WARNING: {name} is empty, skipping")
            continue
        resp = cf_request("PUT",
            f"/accounts/{account_id}/workers/scripts/thiel-feedback/secrets",
            data={"name": name, "text": value, "type": "secret_text"}
        )
        if resp.get("success"):
            print(f"Secret {name}: OK")
        else:
            print(f"WARNING setting {name}: {resp.get('errors')}")

    # Step 4: Enable workers.dev subdomain
    cf_request("POST",
        f"/accounts/{account_id}/workers/scripts/thiel-feedback/subdomain",
        data={"enabled": True}
    )

    # Step 5: Get subdomain
    resp = cf_request("GET", f"/accounts/{account_id}/workers/subdomain")
    subdomain = (resp.get("result") or {}).get("subdomain", "unknown")

    print()
    print("✅ Worker deployed successfully!")
    print(f"🌐 Worker URL: https://thiel-feedback.{subdomain}.workers.dev")
    print()
    print("➡️  Jetzt als GitHub Secret setzen:")
    print("   Name:  WORKER_URL")
    print(f"   Value: https://thiel-feedback.{subdomain}.workers.dev")


if __name__ == "__main__":
    main()
