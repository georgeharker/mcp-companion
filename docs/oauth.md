# OAuth Authentication — Design & Implementation

## Overview

The bridge supports OAuth 2.1 Authorization Code + PKCE for upstream MCP servers
that require user authentication (e.g. Google Workspace via workspace-mcp, ClickUp).
All OAuth logic lives in the Python bridge (`bridge/mcp_bridge/`); the Rust
`sharedserver` binary is only a process supervisor and has no OAuth involvement.

The implementation is built on three layers:

1. **MCP SDK** (`mcp.client.auth.oauth2.OAuthClientProvider`) — the core httpx
   `Auth` subclass that implements the RFC 9728 discovery + Authorization Code +
   PKCE flow as a request/response generator.
2. **FastMCP** (`fastmcp.client.auth.OAuth`) — wraps the MCP SDK provider with
   token storage adapters, static client info support, and a uvicorn-based OAuth
   callback server.
3. **Bridge** (`mcp_bridge.auth._RefreshTokenOAuth`) — our subclass of FastMCP's
   `OAuth` that fixes several upstream issues and adds Google-specific behaviour.

## Configuration

OAuth is configured per-server in the MCP servers config file
(`~/.config/secrets/mcpservers.json` or equivalent):

```json
{
  "servers": {
    "gws": {
      "url": "http://localhost:8002/mcp",
      "auth": {
        "oauth": {
          "client_id": "...",
          "client_secret": "...",
          "callback_port": 9876,
          "scopes": ["..."],
          "cache_tokens": true
        }
      }
    }
  }
}
```

The `auth` field supports three forms:

| Form | Behaviour |
|------|-----------|
| `"oauth"` | Default OAuth flow, no pre-registered client |
| `{"oauth": {...}}` | OAuth with explicit client_id, scopes, etc. |
| `{"bearer": "token"}` | Static Bearer token (no OAuth) |

Global settings in the bridge config:

| Setting | Default | Description |
|---------|---------|-------------|
| `oauth.cache_tokens` | `true` | Persist tokens to disk (survives restarts) |
| `oauth.token_dir` | `~/.cache/mcp-companion/oauth-tokens` | Token storage root |

Per-server `cache_tokens` overrides the global setting.

## Token Storage

### Location

```
~/.cache/mcp-companion/oauth-tokens/
└── <server_name>/
    ├── S_mcp_oauth_token-<hash>/
    │   └── S_<server_url>_tokens-<hash>.json       # encrypted OAuthToken
    ├── S_mcp_oauth_client_info-<hash>/
    │   └── S_<server_url>_client_info-<hash>.json   # encrypted client registration
    └── S_mcp_oauth_token_expiry-<hash>/
        └── S_<server_url>_token_expiry-<hash>.json  # encrypted absolute expiry sidecar
```

### Encryption

All token files are encrypted with Fernet (symmetric AES-128-CBC + HMAC-SHA256).

Key derivation priority:

1. **`MCP_BRIDGE_TOKEN_KEY` env var** — set via Lua `config.bridge.token_key` or
   directly. Derives a Fernet key via `derive_jwt_key(material, salt)`.
2. **Machine ID + username fallback** — `f"{platform.node()}:{getpass.getuser()}:mcp-companion-tokens"`
   with the same derivation. Stable across restarts but provides obfuscation, not
   strong security.

The store uses `FernetEncryptionWrapper` with `raise_on_decryption_error=False` —
if the encryption key changes, old tokens silently become unreadable (cache miss),
triggering a fresh OAuth flow rather than crashing.

### What Gets Stored

| Collection | Key | Contents | TTL |
|------------|-----|----------|-----|
| `mcp-oauth-token` | `{server_url}/tokens` | `OAuthToken` (access_token, refresh_token, expires_in, scope) | 1 year |
| `mcp-oauth-client-info` | `{server_url}/client_info` | `OAuthClientInformationFull` (dynamic client registration) | Until `client_secret_expires_at` |
| `mcp-oauth-token-expiry` | `{server_url}/token_expiry` | `{"expires_at": <absolute_timestamp>}` | 1 year |

The `mcp-oauth-token-expiry` sidecar is a bridge-level addition (see [Token Expiry
Problem](#token-expiry-problem-and-sidecar-fix)).

## Auth Selection

`build_auth()` in `auth.py` is the factory:

```python
def build_auth(server_name, *, auth_config, server_url, token_dir, cache_tokens):
    if auth_config is None:
        return None                          # no auth
    if auth_config == "oauth":
        return _build_oauth(...)             # default OAuth
    if "bearer" in auth_config:
        return _BearerAuth(token)            # static token
    if "oauth" in auth_config:
        return _build_oauth(...)             # explicit OAuth opts
```

`_build_oauth()` creates a `_RefreshTokenOAuth` instance with an encrypted file
store (or `None` for in-memory). The `_RefreshTokenOAuth` class is the heart of
the bridge's OAuth handling.

## OAuth Flow Lifecycle

### On Bridge Startup (cached tokens exist)

```
_make_disconnected_client()
  └→ build_auth() → _build_oauth() → _RefreshTokenOAuth(mcp_url, token_storage, ...)

Client.__aenter__()  (MCP handshake)
  └→ httpx sends request
      └→ _RefreshTokenOAuth.async_auth_flow(request)
          └→ _initialize()  [first call only]
              ├→ super()._initialize()
              │    ├→ Load tokens from disk
              │    └→ Load client_info from disk
              │    └→ update_token_expiry(tokens)  ← WRONG (see below)
              │
              ├→ _discover_oauth_metadata()
              │    ├→ GET server/.well-known/oauth-protected-resource
              │    │    → authorization_servers: ["https://accounts.google.com/"]
              │    └→ GET accounts.google.com/.well-known/oauth-authorization-server
              │         → token_endpoint: "https://oauth2.googleapis.com/token"
              │
              └→ Check sidecar expiry:
                   ├─ Valid → use cached token
                   ├─ Expired → _proactive_refresh()  (silent HTTP POST)
                   └─ No sidecar → _proactive_refresh()  (bootstrap)
```

### On Token Expiry (during normal operation)

```
httpx sends request
  └→ async_auth_flow(request)
      ├→ is_token_valid()? → No (token_expiry_time < now)
      ├→ can_refresh_token()? → Yes (refresh_token + client_info exist)
      └→ POST token_endpoint with grant_type=refresh_token
          ├─ 200 OK → new tokens stored, continue
          └─ Non-200 → tokens cleared → next request gets 401 → full re-auth
```

### On Full Re-authorization (browser flow)

```
Request returns 401
  └→ async_auth_flow detects 401
      ├→ Discover protected resource metadata (RFC 9728)
      ├→ Discover OAuth AS metadata
      ├→ Register client (dynamic registration or CIMD) if needed
      ├→ redirect_handler(authorization_url)
      │    └→ [Google] inject access_type=offline + prompt=consent
      │    └→ Open browser
      ├→ callback_handler()
      │    └→ Start uvicorn on callback_port (e.g. 9876)
      │    └→ Wait for OAuth redirect with auth code (300s timeout)
      ├→ Exchange code for tokens (POST token_endpoint)
      └→ Store tokens + persist sidecar expiry
```

## Problems Found and Fixes

### 1. OAuth Metadata Not Persisted (Token Refresh 404)

**Problem**: The MCP SDK's `_initialize()` loads tokens and client_info from disk
but **not** `oauth_metadata`. Without it, `_get_token_endpoint()` falls back to
`urljoin(get_authorization_base_url(server_url), "/token")`. For a proxy server at
`http://localhost:8002/mcp`, this produces `http://localhost:8002/token` — which
does not exist. The 404 response causes the SDK to clear all cached tokens and
trigger a full browser re-authorization.

**Fix**: `_RefreshTokenOAuth._initialize()` calls `_discover_oauth_metadata()` after
loading tokens. This performs RFC 9728 protected-resource discovery followed by OAuth
AS metadata discovery:

```
server_url → .well-known/oauth-protected-resource → authorization_servers[0]
           → .well-known/oauth-authorization-server → token_endpoint
```

The discovered `token_endpoint` (e.g. `https://oauth2.googleapis.com/token`) is set
on `ctx.oauth_metadata`, so `_get_token_endpoint()` uses the correct URL for all
subsequent refresh requests.

This is a generic fix — it works for any OAuth-proxied MCP server, not just Google.
For example, ClickUp's proxy at `https://mcp.clickup.com/mcp` correctly discovers
`token_endpoint=https://mcp.clickup.com/oauth/token`.

**Why not cache metadata to disk?** Discovery is cheap (local HTTP + one HTTPS to
the provider, ~100-200ms). Metadata can change. Cache invalidation would add
complexity with minimal benefit.

### 2. Token Expiry Problem and Sidecar Fix

**Problem**: `expires_in` in the `OAuthToken` is a relative value (e.g. `3600`
seconds from issuance). The MCP SDK stores it as-is. On reload, FastMCP's
`_initialize()` recalculates `token_expiry_time = time.time() + expires_in`, making
a token issued hours ago appear freshly minted. The SDK then sends the expired access
token, gets a 401, and triggers a full browser re-auth — even though a silent refresh
would have worked.

**Fix**: The bridge persists an absolute expiry timestamp in a sidecar key
(`mcp-oauth-token-expiry` collection) alongside the token. On init:

1. **Sidecar exists, token not expired** → use cached access token as-is.
2. **Sidecar exists, token expired** → proactively refresh via `_proactive_refresh()`.
3. **No sidecar (bootstrap)** → proactively refresh to get a fresh token and
   establish the sidecar.

`_proactive_refresh()` POSTs directly to the real token endpoint with
`grant_type=refresh_token`, updates the context, stores new tokens, and persists the
sidecar — all during `_initialize()`, before any MCP requests are made.

The sidecar is also updated after every `async_auth_flow()` completion (which covers
SDK-initiated refreshes and full re-authorizations during normal operation).

### 3. Concurrent OAuth Callback Server Crash

**Problem**: When the upstream MCP server goes down and recovers, both the initial
connection and the health-check monitor can trigger reconnection. Each reconnect
creates a new `_RefreshTokenOAuth` instance, each of which may start a browser-based
OAuth flow. Both flows try to start a uvicorn callback server on the same port
(e.g. 9876). The second bind fails, uvicorn calls `sys.exit(1)`, and the
`SystemExit` propagates through anyio's TaskGroup, killing the entire bridge.

**Fix** (two layers):

1. **`callback_handler()` singleton** (in `_RefreshTokenOAuth`): A class-level
   registry (`_active_flows`) tracks which ports have an active OAuth callback server.
   If a second OAuth flow tries the same port, it waits for the first flow's result
   instead of launching a duplicate server.

2. **`SystemExit` catch in `_open()`** (in `ConnectionManager`): Defense-in-depth —
   catches `SystemExit` from uvicorn's port bind failure and treats it as a transient
   error (health-check will retry later) instead of letting it kill the bridge.

### 4. Google Refresh Token

**Problem**: Google's OAuth only issues a `refresh_token` when the authorization
request includes `access_type=offline`. The standard MCP OAuth flow does not include
this parameter, so Google issues only a short-lived access token. Without a refresh
token, the bridge must open a browser every time the token expires (~1 hour).

**Fix**: `redirect_handler()` inspects the authorization URL. If the host is
`accounts.google.com`, it injects `access_type=offline` and `prompt=consent` into
the query parameters. `prompt=consent` forces Google to re-issue a refresh token
even when the user previously consented.

This is the only Google-specific code in the bridge. All other fixes are generic
and work with any OAuth provider.

## Connection Manager Integration

`ConnectionManager` in `connections.py` manages persistent `Client` sessions:

| Method | Role |
|--------|------|
| `_open(conn)` | **Only** place that creates Client + OAuth instance |
| `_monitor(conn)` | Health-check loop (30s interval), triggers `_reconnect()` |
| `_reconnect(conn)` | Closes old session, sleeps backoff, calls `_open()` |
| `get_client_factory(name)` | Returns closure — never creates new OAuth |

Auth-failure semantics:
- OAuth is attempted **once** per server during `connect_all()`.
- If it fails → `_auth_failed=True`, monitor stops retrying, factory raises
  `AuthenticationError`.
- **Only recovery**: `bridge__enable_server` meta-tool → `reset_auth_failure()` →
  `connect()` for a single fresh attempt.

## Stale Client Detection

During tool execution, `ToolProcessingMiddleware.on_call_tool()` (in `server.py`)
catches errors that suggest the OAuth server lost the client registration. If
`is_stale_client_error(e)` matches (checks for strings like "unregistered client",
"invalid_client", "client not found"), it calls `clear_oauth_cache(server_name)`
which deletes the entire server's token directory. The next connection attempt
triggers a fresh OAuth flow with new registration.

## Decision Matrix: Refresh vs Re-auth

| Condition | Outcome |
|-----------|---------|
| Valid access token (not expired) | **Use as-is** — Bearer header added |
| Expired + refresh_token exists | **Silent refresh** — POST to token endpoint |
| Refresh succeeds | **Continue** — new tokens + sidecar persisted |
| Refresh fails (non-200) | **Tokens cleared** → full re-auth on next 401 |
| No tokens at all (first run, cache cleared, key changed) | **Full re-auth** — browser opens |
| No refresh_token | **Full re-auth** on every token expiry |
| Server returns 401 | **Full re-auth** — discovery + registration + browser |
| Server returns 403 + insufficient_scope | **Re-auth with updated scopes** |
| Stale client (ClientNotFoundError) | **Clear cache + retry** with fresh registration |
| Stale client during tool call | **Clear cache directory** → full re-auth on next attempt |
| Auth failure on startup | **Marked `_auth_failed`** — no auto-retry; manual `bridge__enable_server` |
| Bridge restart with cached tokens | **Proactive refresh** during `_initialize()` — no browser |

## Key Functions Reference

### auth.py

```python
class _RefreshTokenOAuth(OAuth):
    """Full OAuth subclass with all bridge-level fixes."""

    # Class-level state for callback server singleton
    _active_flows: ClassVar[dict[int, tuple[anyio.Event, OAuthCallbackResult]]]
    _flow_lock: ClassVar[asyncio.Lock]

    async def callback_handler(self) -> tuple[str, str | None]
        # Per-port singleton — reuses existing callback server

    async def _save_token_expiry(self) -> None
        # Persist absolute expiry to sidecar key

    async def _load_token_expiry(self) -> float | None
        # Load absolute expiry from sidecar key

    async def _proactive_refresh(self) -> None
        # Direct POST to token endpoint during init

    async def _initialize(self) -> None
        # Load tokens, discover metadata, restore/refresh expiry

    async def async_auth_flow(self, request) -> AsyncGenerator
        # Wraps parent to persist sidecar after flow completes

    async def _discover_oauth_metadata(self) -> None
        # RFC 9728 PRM + OAuth AS metadata discovery

    async def redirect_handler(self, authorization_url: str) -> None
        # Injects access_type=offline for Google

def build_auth(server_name, *, auth_config, server_url, ...) -> httpx.Auth | None
    # Factory: oauth → _RefreshTokenOAuth, bearer → _BearerAuth, None

def clear_oauth_cache(server_name, token_dir) -> bool
    # Deletes entire server token directory

def is_stale_client_error(error) -> bool
    # Substring matching for stale registration errors
```

### connections.py

```python
class ConnectionManager:
    async def _open(conn)           # Creates Client+OAuth, catches SystemExit
    async def _reconnect(conn)      # Close + backoff + _open()
    async def _monitor(conn)        # Health-check loop, skips auth-failed
    def get_client_factory(name)    # Returns closure, never creates OAuth
    def reset_auth_failure(name)    # Manual recovery path

def _is_auth_error(exc) -> bool    # OAuthFlowError, ClientNotFoundError, 401/403
```

## Setup Guide: External OAuth Provider (Google Workspace Example)

This section walks through setting up an MCP server that uses an external OAuth
provider. Google Workspace via `workspace-mcp` is used as the example, but the
pattern applies to any external OAuth 2.1 provider.

### Prerequisites

- A Google Cloud project with the OAuth consent screen configured
- `workspace-mcp` package available (installed via `uvx` or `pip`)
- 1Password CLI (`op`) if using 1Password references for secrets (optional)

### Step 1: Create a Google Cloud OAuth Client

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials)
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Web application**
4. Add an **Authorized redirect URI**: `http://localhost:9876/callback`
   (must match the `callback_port` in your config)
5. Note the **Client ID** and **Client Secret**

> **Scopes**: workspace-mcp requests its own scopes from Google. You do not need
> to configure scopes in the bridge — they come from the upstream server's
> `.well-known/oauth-protected-resource` metadata.

### Step 2: Enable Google APIs

In the Google Cloud Console, enable the APIs that workspace-mcp needs:

- Google Drive API
- Gmail API
- Google Calendar API
- Google Docs API
- Google Sheets API
- Google Slides API
- Google Forms API
- Google Chat API
- People API (Contacts)
- Google Tasks API
- Apps Script API
- Custom Search API (if using search)

The exact set depends on which workspace-mcp tools you plan to use.

### Step 3: Configure the Shared Server

The shared server manages the `workspace-mcp` process lifecycle. Add it to your
MCP servers config file (e.g. `~/.config/secrets/mcpservers.json`):

```json
{
  "sharedServers": {
    "goog_ws": {
      "command": "uvx",
      "args": ["workspace-mcp", "--transport", "streamable-http"],
      "env": {
        "WORKSPACE_MCP_PORT": "8002",
        "MCP_ENABLE_OAUTH21": "true",
        "EXTERNAL_OAUTH21_PROVIDER": true,
        "WORKSPACE_MCP_STATELESS_MODE": true,
        "GOOGLE_OAUTH_CLIENT_ID": "<your-client-id>",
        "GOOGLE_OAUTH_CLIENT_SECRET": "<your-client-secret>",
        "WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND": "disk",
        "OAUTHLIB_INSECURE_TRANSPORT": "1"
      },
      "grace_period": "3m",
      "health_timeout": 30
    }
  }
}
```

**Environment variables explained:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `WORKSPACE_MCP_PORT` | `8002` | HTTP port workspace-mcp listens on |
| `MCP_ENABLE_OAUTH21` | `"true"` | Enable OAuth 2.1 on the MCP endpoint |
| `EXTERNAL_OAUTH21_PROVIDER` | `true` | Use Google as external OAuth provider (not built-in) |
| `WORKSPACE_MCP_STATELESS_MODE` | `true` | Stateless HTTP mode (no session state between requests) |
| `GOOGLE_OAUTH_CLIENT_ID` | Client ID | From Step 1 |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Client secret | From Step 1 |
| `WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND` | `"disk"` | Persist proxy state to disk |
| `OAUTHLIB_INSECURE_TRANSPORT` | `"1"` | Allow HTTP (not HTTPS) for localhost transport |

**Shared server options:**

| Option | Description |
|--------|-------------|
| `grace_period` | How long to keep the server alive after last client disconnects |
| `health_timeout` | Seconds to wait for health check response before considering server down |

### Step 4: Configure the MCP Server Entry

Add the server entry that connects to workspace-mcp via the shared server:

```json
{
  "mcpServers": {
    "gws": {
      "url": "http://localhost:8002/mcp",
      "auth": {
        "oauth": {
          "client_id": "<your-client-id>",
          "client_secret": "<your-client-secret>",
          "callback_port": 9876
        }
      },
      "sharedServer": "goog_ws"
    }
  }
}
```

**Fields:**

| Field | Description |
|-------|-------------|
| `url` | The MCP endpoint URL (must match `WORKSPACE_MCP_PORT` + `/mcp` path) |
| `auth.oauth.client_id` | Same Google OAuth client ID as the shared server |
| `auth.oauth.client_secret` | Same client secret |
| `auth.oauth.callback_port` | Port for the OAuth redirect callback (must match the authorized redirect URI) |
| `sharedServer` | References the shared server name from `sharedServers` |

### Using 1Password References

Instead of hardcoding secrets, you can use 1Password secret references:

```json
{
  "auth": {
    "oauth": {
      "client_id": "op://API/GoogleAPI/client_id",
      "client_secret": "op://API/GoogleAPI/credential",
      "callback_port": 9876
    }
  }
}
```

The bridge resolves `op://` references via the 1Password CLI before use.

### Step 5: First Run

On first launch, the bridge will:

1. Start the `workspace-mcp` shared server process
2. Discover OAuth metadata from `http://localhost:8002/.well-known/oauth-protected-resource`
3. Open your browser to Google's consent screen
4. You authorize the requested scopes
5. The bridge receives the auth code on `http://localhost:9876/callback`
6. Tokens are exchanged, encrypted, and stored to disk

Subsequent restarts silently refresh the cached token — no browser interaction
required (see [Token Expiry Problem and Sidecar Fix](#token-expiry-problem-and-sidecar-fix)).

### Step 6: Verify

Check that the server connected successfully:

```
# In Neovim, use the bridge status meta-tool
:lua require('mcp_companion').call_tool('bridge__status')
```

Or check the bridge log at `~/.local/state/nvim/mcp-bridge.log` for:
```
Persistent connection opened: gws
```

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Browser opens on every restart | Token refresh failing — check log for `Token refresh failed` | Ensure metadata discovery works (check `.well-known` endpoints) |
| `OAuth callback timed out after 300.0 seconds` | Browser flow not completed within 5 minutes | Complete the consent flow faster, or re-enable the server |
| `[Errno 48] address already in use` on callback port | Previous callback server still running | Wait for it to timeout, or restart the bridge |
| `401 Unauthorized` on tool calls | Access token expired and refresh failed | Check that `access_type=offline` is being injected (bridge handles this automatically for Google) |
| `Token refresh failed: 404` | Token endpoint URL incorrect | Ensure workspace-mcp's `.well-known/oauth-protected-resource` returns correct `authorization_servers` |
| Server shows `_auth_failed` | OAuth failed on startup and was not retried | Use `bridge__enable_server` meta-tool to retry |

### Other OAuth Providers

The same pattern works for any MCP server that implements RFC 9728
(Protected Resource Metadata). The bridge auto-discovers the OAuth endpoints:

```
server_url → .well-known/oauth-protected-resource
  → authorization_servers[0]
    → .well-known/oauth-authorization-server
      → token_endpoint, authorization_endpoint
```

Provider-specific notes:
- **Google**: Bridge automatically injects `access_type=offline` +
  `prompt=consent` for refresh token support
- **ClickUp**: Works out of the box — ClickUp's MCP proxy handles its own
  endpoint discovery
- **GitHub**: Uses static Bearer token (`auth.bearer`), not OAuth

## Upstream SDK Issues

These are known issues in the vendored MCP SDK and FastMCP that the bridge works
around:

1. **oauth_metadata not persisted** — `OAuthClientProvider._initialize()` loads
   tokens but not metadata. Fixed by `_discover_oauth_metadata()`.

2. **expires_in treated as absolute on reload** — `calculate_token_expiry()` uses
   `time.time() + expires_in` which is correct at issuance but wrong on reload.
   Fixed by sidecar expiry persistence.

3. **uvicorn sys.exit(1) on port conflict** — The OAuth callback server (uvicorn)
   calls `sys.exit(1)` when it can't bind the port, killing the entire process.
   Fixed by `SystemExit` catch in `_open()` and callback singleton.

4. **No offline access for Google** — The standard flow doesn't request
   `access_type=offline`, so Google doesn't issue a refresh_token. Fixed by
   `redirect_handler()`.
