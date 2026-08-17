#!/usr/bin/env python3
"""One-time OAuth2 flow to get a OneDrive (Microsoft Graph) refresh token.

Usage:
    python3 scripts/onedrive_oauth.py --client-id CLIENT_ID --client-secret CLIENT_SECRET

Opens a browser for Microsoft login, catches the redirect, and exchanges
the code for access + refresh tokens. The refresh token is what you store
in mediasync.env — it auto-renews on each use (90-day rolling expiry).
"""

import argparse
import http.server
import json
import urllib.parse
import urllib.request
import webbrowser

AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPES = "Files.ReadWrite offline_access"


def main():
    parser = argparse.ArgumentParser(description="OneDrive OAuth2 token generator")
    parser.add_argument("--client-id", required=True, help="Azure app client ID")
    parser.add_argument("--client-secret", required=True, help="Azure app client secret")
    parser.add_argument("--port", type=int, default=53682, help="Local redirect port")
    args = parser.parse_args()

    redirect_uri = f"http://localhost:{args.port}/"
    auth_result = {"code": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)

            if "code" in params:
                auth_result["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Success!</h1><p>You can close this tab.</p>")
            else:
                error = params.get("error_description", params.get("error", ["unknown"]))[0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<h1>Error: {error}</h1>".encode())

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("localhost", args.port), Handler)

    # Build authorization URL
    auth_url = (
        f"{AUTHORITY}/authorize?"
        f"client_id={args.client_id}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&scope={urllib.parse.quote(SCOPES)}"
        f"&response_mode=query"
    )

    print("\nOpening browser for Microsoft authorization...")
    print(f"If browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for authorization...")
    server.handle_request()
    server.server_close()

    if not auth_result["code"]:
        print("\nAuthorization failed or was denied.")
        return

    # Exchange code for tokens
    print("Exchanging code for tokens...")
    token_data = urllib.parse.urlencode({
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "code": auth_result["code"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "scope": SCOPES,
    }).encode()

    req = urllib.request.Request(
        f"{AUTHORITY}/token",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"\nToken exchange failed ({e.code}): {error_body}")
        return
    except Exception as e:
        print(f"\nToken exchange failed: {e}")
        return

    if "error" in data:
        print(f"\nError: {data['error_description']}")
        return

    access_token = data["access_token"]
    refresh_token = data.get("refresh_token", "")

    # Verify by fetching user info
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0/me/drive",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            drive_info = json.loads(resp.read().decode())
        owner = drive_info.get("owner", {}).get("user", {}).get("displayName", "?")
        quota = drive_info.get("quota", {})
        total_gb = quota.get("total", 0) / (1024**3)
        used_gb = quota.get("used", 0) / (1024**3)
    except Exception:
        owner = "unknown"
        total_gb = used_gb = 0

    print(f"\n{'='*60}")
    print("  SUCCESS! OneDrive OAuth2 tokens obtained.")
    print(f"{'='*60}")
    print(f"  Owner:         {owner}")
    print(f"  Storage:       {used_gb:.1f} GB / {total_gb:.1f} GB")
    print(f"  Refresh Token: {refresh_token[:20]}...{refresh_token[-10:]}")
    print(f"{'='*60}")
    print("\nAdd these to your mediasync.env:")
    print(f"  MEDIASYNC_ONEDRIVE_CLIENT_ID={args.client_id}")
    print(f"  MEDIASYNC_ONEDRIVE_CLIENT_SECRET={args.client_secret}")
    print(f"  MEDIASYNC_ONEDRIVE_REFRESH_TOKEN={refresh_token}")
    print("\nThe refresh token auto-renews on use (90-day rolling expiry).")


if __name__ == "__main__":
    main()
