# Security

## Docker socket

Docker monitoring mounts `/var/run/docker.sock`. The `:ro` flag only protects the socket file, **not** the
Docker API: anyone who can talk to the socket is effectively root on the host. Moon Watcher only uses
read-only calls (list / inspect / stats / logs), but a compromised bot would still hold that access.

For stricter isolation put a [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)
in front of it with `CONTAINERS=1`, remove the socket volume from `docker-compose.yml`
and set `DOCKER_HOST=tcp://socket-proxy:2375` in `.env`.
If you don't need container monitoring, set `DOCKER_MONITORING=false` and remove the socket mount.

## Access control

Only Telegram user IDs listed in `ADMIN_USER_IDS` can use the bot. Keep `.env` private and never commit it.
If a bot token leaks, revoke it in @BotFather.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Report a vulnerability) instead of a public issue.
