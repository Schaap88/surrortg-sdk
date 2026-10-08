# Local Controller configuration service

The production non-secret file is `/etc/srtg/controller.toml` (mode `0640`),
the replace-only credential is `/etc/srtg/controller.credential` (mode `0600`),
and the dedicated runtime socket is `/run/srtg/controller-management.sock`
(mode `0660`). Without a writable systemd runtime directory, it safely falls
back to a per-user `0700` directory under `$XDG_RUNTIME_DIR` (or `/tmp`). Paths
are constructor/environment configurable for development.

The schema-1 TOML contains `device_id`, `[signaling].endpoint`, `[game].id`,
and optional `[runtime].module`. Writes are validated, revision-checked, written
through a same-directory temporary file, fsynced, renamed, directory-fsynced,
reread, and accompanied by `controller.toml.last-good`. Credentials never
appear in sanitized configuration or status.

An explicit legacy `-c` file always wins for that process and is not modified.
Without `-c`, an existing new-format configuration wins. Otherwise
`/etc/srtg/srtg.toml` is validated and imported; both split `id`/`token` and
legacy `GAME_ID/TOKEN` token syntax are supported. Legacy is import-only and is
left intact.

The newline-delimited JSON Unix-socket commands are exactly `status` and
`reconnect`. Status is observational and sanitized. A missing, refused, stale,
or malformed runtime socket is reported as `runtime_unavailable`; it does not
prevent configuration access. Reconnect only cycles Socket.IO transport and is
idempotent. No gameplay, Robot actuation, shell, power, or authoritative Game
mutation command exists.

## Local web interface

Run the separate aiohttp service with:

```bash
python -m surrortg.local_web
```

It binds to `127.0.0.1:8088` by default. `--host`, `--port`, `--config`,
`--credential`, `--admin-hash`, `--management-socket`, and repeatable
`--allowed-host` options make appliance paths and LAN binding explicit.
`SURRORTG_WEB_HOST`, `SURRORTG_WEB_PORT`, `SURRORTG_CONFIG_PATH`,
`SURRORTG_CREDENTIAL_PATH`, `SURRORTG_ADMIN_HASH_PATH`, and
`SURRORTG_MANAGEMENT_SOCKET` are the equivalent environment settings. Add
`--secure-cookie` behind HTTPS. Local HTTP commissioning deliberately leaves
the cookie's Secure flag off, while retaining `HttpOnly` and `SameSite=Strict`.
TLS provisioning is outside this service.

On first production start, the service generates a random administrator
password, stores only its PBKDF2-SHA256 hash in
`/etc/srtg/local-web-admin.hash` (mode `0600`), and prints the password once.
There is no default password. `--development-admin-password` is an explicit,
development-only way to create the first hash; it has no effect after the hash
exists. Sessions are opaque, process-local, expire after one hour, and all
writes require a per-session CSRF token. Login attempts are rate-limited.

The authenticated pages are Overview, Configuration, and Diagnostics.
Configuration uses the store's optimistic revision and atomic writes. An empty
credential input preserves the existing credential, and neither HTML nor the
allow-listed JSON diagnostic export can return it. A saved endpoint, Game ID,
or credential change offers a POST-only **Apply and reconnect** action. Device
ID or runtime-module changes are correctly reported as requiring a runtime
restart; the web service never performs that restart.

Runtime status and reconnect use only the bounded management IPC. Missing,
refused, malformed, or timed-out sockets render as a useful runtime-unavailable
state. Configuration remains readable and writable while the runtime is down.
Unexpected Host headers and cross-origin requests are rejected; permissive CORS
is not enabled.
