import os
import time
import json
import webbrowser
import ssl
import threading
import http.server
import socketserver
import urllib.parse
from typing import Optional
import re
from utils import vprint

# You need to install: cryptography
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from datetime import datetime, timedelta

TOKEN_FILE = "twitch_token.json"
CERT_FILE = "localhost.pem"
KEY_FILE = "localhost-key.pem"

def get_selfsigned_cert(certfile: str = CERT_FILE, keyfile: str = KEY_FILE):
    """
    Generate a self-signed certificate for HTTPS localhost if not present.
    """
    if not (os.path.exists(certfile) and os.path.exists(keyfile)):
        print("[CERT] Generating new self-signed cert for localhost...")
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, u"US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"California"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, u"San Francisco"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"Localhost"),
            x509.NameAttribute(NameOID.COMMON_NAME, u"localhost"),
        ])
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.utcnow()
        ).not_valid_after(
            datetime.utcnow() + timedelta(days=3650)
        ).add_extension(
            x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
            critical=False,
        ).sign(key, hashes.SHA256())
        # Write private key
        with open(keyfile, "wb") as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        # Write cert
        with open(certfile, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        print(f"[CERT] Created self-signed cert: {certfile}, key: {keyfile}")
    return certfile, keyfile

def load_twitch_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    return {}

def save_twitch_token(data):
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)

class TwitchOAuthTokenManager:
    AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    REDIRECT_URI = "https://localhost:8443/callback"
    # Required scopes to run the moderation bot. These allow reading chat,
    # sending messages as the authenticated user, and managing bans/timeouts.
    SCOPE = (
        "chat:read "
        "user:read:chat "
        "user:write:chat "
        "moderator:manage:chat_messages "
        "moderator:manage:banned_users"
    )

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_data = load_twitch_token()
        # Serialize access to refresh/authorize to prevent multiple threads
        # from triggering concurrent OAuth flows.
        self._lock = threading.Lock()

    def get_token(self) -> str:
        """
        Get a valid access token, refreshing or authorizing if necessary.
        """
        with self._lock:
            vprint(2, f"[OAUTH DEBUG] Loaded token: {self.token_data}")
            if (self.token_data
                and self.token_data.get("access_token")
                and self.token_data.get("expires_at", 0) > time.time() + 120):
                vprint(1, "[OAUTH] Using cached token.")
                return self.token_data["access_token"]

            if self.token_data and self.token_data.get("refresh_token"):
                try:
                    vprint(1, "[OAUTH] Attempting to refresh token.")
                    self.refresh_token()
                    if self.token_data.get("access_token"):
                        return self.token_data["access_token"]
                except Exception as e:
                    print(f"[OAUTH ERROR] Refresh failed: {e}")

            vprint(1, "[OAUTH] No valid token. Starting authorization flow...")
            self.authorize()
            if self.token_data and self.token_data.get("access_token"):
                return self.token_data["access_token"]
            raise RuntimeError("OAuth failed: No access token obtained!")

    def authorize(self):
        """
        Launch browser OAuth flow, start HTTPS server for callback,
        and save received token.
        """
        # Ensure self-signed cert exists
        certfile, keyfile = get_selfsigned_cert()
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.REDIRECT_URI,
            "response_type": "code",
            "scope": self.SCOPE,
        }
        url = f"{self.AUTH_URL}?{urllib.parse.urlencode(params)}"
        print(f"\n[TWITCH OAUTH] Please open this link to authorize (if browser doesn't open automatically):\n  {url}\n")
        webbrowser.open(url)
        code_holder = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                if parsed.path == "/callback" and "code" in qs:
                    code_holder["code"] = qs["code"][0]
                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<h1>Authorization successful!</h1><p>You may close this window.</p>")
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authorization failed or was cancelled.")
            def log_message(self, *a, **k): pass

        class ReusableTCPServer(socketserver.TCPServer):
            allow_reuse_address = True

        try:
            with ReusableTCPServer(("localhost", 8443), Handler) as httpd:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ssl_context.load_cert_chain(certfile=certfile, keyfile=keyfile)
                httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
                t = threading.Thread(target=httpd.serve_forever)
                t.daemon = True
                t.start()
                vprint(1, "[OAUTH] Waiting for browser callback on https://localhost:8443/callback ...")
                for _ in range(300):
                    if "code" in code_holder:
                        break
                    time.sleep(1)
                httpd.shutdown()
        except Exception as e:
            vprint(1, f"[OAUTH] Local callback server error: {e}")
        if "code" not in code_holder:
            vprint(1, "[OAUTH] Callback not received.\n"
                  "If the browser opened, copy the full URL you were redirected to"
                  " and paste it below.")
            manual_url = input("Paste redirect URL (or press Enter to abort): ").strip()
            if manual_url:
                try:
                    parsed = urllib.parse.urlparse(manual_url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    if "code" in qs:
                        code_holder["code"] = qs["code"][0]
                except Exception:
                    pass
        if "code" not in code_holder:
            raise RuntimeError("OAuth failed: Did not receive code.")
        vprint(1, "[OAUTH] Received code. Requesting token...")
        import requests
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code_holder["code"],
            "grant_type": "authorization_code",
            "redirect_uri": self.REDIRECT_URI,
        }
        resp = requests.post(self.TOKEN_URL, data=data)
        try:
            resp.raise_for_status()
        except Exception as e:
            print(f"[OAUTH ERROR] Token request failed: {resp.text}")
            raise
        token_json = resp.json()
        self.token_data = {
            "access_token": token_json["access_token"],
            "refresh_token": token_json.get("refresh_token"),
            "expires_at": time.time() + token_json.get("expires_in", 0)
        }
        save_twitch_token(self.token_data)
        vprint(1, "[OAUTH] Token obtained and saved.")

    def refresh_token(self):
        """
        Uses the refresh_token to obtain a new access token.
        """
        vprint(1, "[OAUTH] Refreshing token...")
        import requests
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.token_data["refresh_token"],
        }
        resp = requests.post(self.TOKEN_URL, data=data)
        try:
            resp.raise_for_status()
        except Exception as e:
            print(f"[OAUTH ERROR] Refresh request failed: {resp.text}")
            raise
        token_json = resp.json()
        self.token_data.update({
            "access_token": token_json["access_token"],
            "refresh_token": token_json.get("refresh_token", self.token_data.get("refresh_token")),
            "expires_at": time.time() + token_json.get("expires_in", 0)
        })
        save_twitch_token(self.token_data)
        vprint(1, "[OAUTH] Token refreshed and saved.")
