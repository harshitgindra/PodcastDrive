#!/usr/bin/env python3
"""One-time OAuth2 flow to get a pCloud access token.

Usage:
    python3 scripts/pcloud_oauth.py --client-id YOUR_ID --client-secret YOUR_SECRET

Opens a browser for authorization, runs a local server to catch the redirect,
and exchanges the code for a permanent access token.
"""

import argparse
import http.server
import json
import threading
import urllib.parse
import urllib.request
import webbrowser


def main():
    parser = argparse.ArgumentParser(description="pCloud OAuth2 token generator")
    parser.add_argument("--client-id", required=True, help="pCloud app client_id")
    parser.add_argument("--client-secret", required=True, help="pCloud app client_secret")
    parser.add_argument("--port", type=int, default=53682, help="Local redirect port")
    args = parser.parse_args()

    redirect_uri = f"http://localhost:{args.port}/"
    auth_result = {"code": None, "hostname": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)

            if "code" in params:
                auth_result["code"] = params["code"][0]
                auth_result["hostname"] = params.get("hostname", ["api.pcloud.com"])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Success!</h1><p>You can close this tab.</p>")
            else:
                error = params.get("error", ["unknown"])[0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<h1>Error: {error}</h1>".encode())
                auth_result["code"] = None

        def log_message(self, format, *args):
            pass  # Suppress log noise

    server = http.server.HTTPServer(("localhost", args.port), Handler)

    # Open browser for authorization
    auth_url = (
        f"https://my.pcloud.com/oauth2/authorize"
        f"?client_id={args.client_id}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
    )

    print(f"\nOpening browser for pCloud authorization...")
    print(f"If browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Wait for redirect
    print("Waiting for authorization...")
    server.handle_request()
    server.server_close()

    if not auth_result["code"]:
        print("\nAuthorization failed or was denied.")
        return

    # Exchange code for token
    hostname = auth_result["hostname"]
    token_url = (
        f"https://{hostname}/oauth2_token"
        f"?client_id={args.client_id}"
        f"&client_secret={args.client_secret}"
        f"&code={auth_result['code']}"
    )

    print(f"Exchanging code for token (via {hostname})...")
    try:
        with urllib.request.urlopen(token_url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"\nToken exchange failed: {e}")
        return

    if data.get("result") != 0:
        print(f"\nError: {data.get('error', 'unknown')}")
        return

    access_token = data["access_token"]
    uid = data.get("uid", "?")

    print(f"\n{'='*60}")
    print(f"  SUCCESS! pCloud OAuth2 token obtained.")
    print(f"{'='*60}")
    print(f"  User ID:      {uid}")
    print(f"  API host:     {hostname}")
    print(f"  Access Token: {access_token}")
    print(f"{'='*60}")
    print(f"\nAdd this to your mediasync.env:")
    print(f"  MEDIASYNC_PCLOUD_TOKEN={access_token}")
    print(f"\nThis token does not expire unless you revoke the app.")


if __name__ == "__main__":
    main()
