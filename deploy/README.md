# Oracle deployment

The application runs as `classroom-bridge.service` on `127.0.0.1:8000`, behind nginx at `https://canigetbus.app`. Only the configured public hostname and local health-check hosts are accepted. HTTPS activates Secure cookies.

- Source: `/srv/classroom-bridge/current` (symlink to a release)
- Python: `/srv/classroom-bridge/venv`
- Secrets: `/etc/classroom-bridge.env`, root:classroom-bridge, mode 0640
- SQLite: `/var/lib/classroom-bridge/classroom.sqlite`, app-owned private directory
- Service unit: `/etc/systemd/system/classroom-bridge.service`
- nginx: `/etc/nginx/sites-available/classroom-bridge`
- Previous nginx configuration backup: `/var/backups/classroom-bridge/`

Register `https://canigetbus.app/auth/google/callback` in the Google OAuth web client's authorized redirect URIs. Keep the local redirect URIs too if local login is still needed. A Google test-mode application also requires the account to be an allowed test user.

The first cloud deployment creates a fresh database with the demo course; local courses and login sessions are not uploaded. The first allowlisted teacher to log in owns the preexisting demo course. Later releases keep the same cloud SQLite path.

The previous `oauth-mcp-test.service` remains running on its existing internal port, but nginx now connects the domain to Classroom Bridge. Classroom Bridge's HTTPS MCP endpoints use OAuth; the previous test server does not participate in that flow. stdio is unsupported.

Check service state with `ssh oracle 'sudo systemctl is-active classroom-bridge'`. Restart after an environment change with `ssh oracle 'sudo systemctl restart classroom-bridge'`. Google callback query parameters are excluded from HTTP access logs.

For rollback, restore the saved nginx symlink in `/etc/nginx/sites-enabled/`, remove the new Classroom Bridge symlink, run `sudo nginx -t`, then reload nginx. Do not delete the cloud SQLite database or environment file. Back up SQLite using its backup API before migrating its schema.


## OAuth verification

MCP URLs are `/mcp/student` and `/mcp/instructor` on the public HTTPS origin. Discovery is served at `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource/mcp/{role}`. The nginx configuration limits registration and token traffic; preserve those limits and disable request access logs when updating the proxy.

The local test suite includes official MCP Python SDK authorization and tool-call round trips, Google callback resumption with a mocked identity provider, one-use PKCE codes, scope/resource binding, rotation/replay detection, revocation, session isolation, and restart persistence. It does not sign in to a real Google account.

`deploy/verify_mcp_oauth.py` is an operational smoke test using a disposable synthetic user and course. Run it under the application account from the project directory with the existing `/etc/classroom-bridge.env` readable. It exercises the real public HTTPS endpoints and FactChat evidence selection, and cleans up its own records in a finally block. It never uses an existing person's Google account or modifies existing courses. A successful run confirms the MCP OAuth protocol; the human Google login/consent screen and individual third-party MCP products still require an account-holder check.


The browser-origin regression test covers ChatGPT-origin MCP/OAuth POSTs and redirect-tainted Origin:null consent. Cookie-authenticated web mutations still require same-origin requests and CSRF, and null-origin consent still needs the user's CSRF form token. MCP CORS accepts exact configured origins without browser credentials; Host validation and bearer auth remain active. The operational smoke script sends these Origin headers explicitly so a no-Origin SDK test cannot mask this interoperability issue.


Consent CSP must include the registered callback origin in form-action, not self alone. An HTTP-only smoke test cannot catch browser CSP redirect blocking. The single-click behavior was verified in an actual browser using isolated localhost/127.0.0.1 origins and a disposable test database, including callback token exchange. Do not relax PKCE/code replay checks to work around a browser remaining on an already-submitted form.
