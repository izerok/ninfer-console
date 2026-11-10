# ninfer-console-restarter

A single-file, stdlib-only sidecar whose entire capability is
restarting **one** container, named by `RESTARTER_CONTAINER` and
resolved by exact name on every call. It exists so the ninfer-console
panel can make engine config changes take effect (the engine reads
its configuration only at startup) without the panel ever touching
the Docker socket.

- `GET /healthz` — Docker reachable? target exists? its state?
- `POST /restart` — restart the target (grace `RESTARTER_TIMEOUT`,
  default 30s). Every other route is a 404; there is no way to name
  a different container.
- Auth: set `RESTARTER_TOKEN`; every request then needs
  `Authorization: Bearer <token>`. The panel side uses
  `PANEL_RESTARTER_TOKEN` with the same value.
- It holds `/var/run/docker.sock` so the panel does not have to.
  Run it only on an internal network — **never publish its port**.

Environment: `RESTARTER_CONTAINER` (required), `RESTARTER_TOKEN`,
`RESTARTER_PORT` (default 8080), `RESTARTER_TIMEOUT` (default 30),
`DOCKER_SOCKET` (default `/var/run/docker.sock`).
