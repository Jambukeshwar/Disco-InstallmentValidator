"""
One-shot OAuth callback server for NSO Validator.

Run this ONCE before logging in:
    python callback_server.py

It starts a temporary HTTP server on port 8080, waits for Salesforce to
redirect back after login, exchanges the code for tokens, saves auth_data.json,
then shuts down automatically. You never need to run it again unless you
delete auth_data.json or your refresh token expires.
"""

import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# salesforce.py must be in the same directory
from salesforce import build_login_url, handle_callback, SalesforceError

PORT = 8080
_server = None  # reference so the handler can shut it down


class CallbackHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path != "/callback":
            self._respond(404, "Not found")
            return

        params = parse_qs(parsed.query)
        code   = params.get("code",  [None])[0]
        state  = params.get("state", [None])[0]
        error  = params.get("error", [None])[0]

        if error:
            self._respond(400, f"""
                <h2>Login failed</h2>
                <p>{params.get('error_description', [error])[0]}</p>
                <p>Close this tab and try again.</p>
            """)
            return

        if not code or not state:
            self._respond(400, "<h2>Missing code or state</h2>")
            return

        try:
            handle_callback(code, state)
            self._respond(200, """
                <h2 style="color:green">✅ Authenticated!</h2>
                <p>You can close this tab and go back to NSO Validator.</p>
                <script>setTimeout(() => window.close(), 2000)</script>
            """)
            print("\n✅  Login successful — auth_data.json saved.")
            print("    You can close this terminal or leave it; it will exit now.\n")
        except SalesforceError as e:
            self._respond(400, f"<h2>Error</h2><p>{e}</p>")
            print(f"\n❌  Login failed: {e}\n")

        # Shut down after handling the one callback
        import threading
        threading.Thread(target=_server.shutdown, daemon=True).start()

    def _respond(self, status: int, html: str):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # suppress default access logs


def main():
    global _server

    login_url = build_login_url()

    print("=" * 60)
    print("  NSO Validator — Salesforce Login")
    print("=" * 60)
    print(f"\nStarting callback listener on http://localhost:{PORT}/callback")
    print("\nOpening Salesforce login in your browser...")
    print("(If the browser doesn't open, paste this URL manually:)")
    print(f"\n  {login_url}\n")

    webbrowser.open(login_url)

    _server = HTTPServer(("localhost", PORT), CallbackHandler)
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)


if __name__ == "__main__":
    main()
