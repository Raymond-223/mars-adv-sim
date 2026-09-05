# Optional Docker deployment

Docker is optional; the Windows desktop-style launcher is the primary competition demo path.

The Compose profile is secure-by-default for the v1.9.0 release:
- PostgreSQL, Redis and MQTT are internal-only (no host ports published).
- PostgreSQL/Redis/MQTT secrets must be supplied explicitly via environment variables.
- MQTT anonymous access is disabled.
- The console is bound to host loopback only.
- The workspace is writable because the control plane must persist settings, SQLite/EventStore state and evidence.

Required variables before `docker compose`:
`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `MQTT_USERNAME`, `MQTT_PASSWORD`.

The in-Compose MQTT broker is not exposed outside the Compose network. For a physical remote Agent on another machine, configure a separate broker with TLS and credentials, then register it in Settings > Remote execution nodes with TLS enabled.
