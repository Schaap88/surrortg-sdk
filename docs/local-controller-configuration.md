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
