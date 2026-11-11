# Security policy

## Supported versions
Only the latest tagged version receives fixes. `v0.x` is local-only software that is not published.

## Reporting a vulnerability
Do not open a public issue. Write privately to **security@numen.games** with: the version (`plugin.yaml`),
what you did, what you expected and what happened. You get an acknowledgement within 5 working days.
Please allow 90 days before public disclosure; we will credit you if you wish.

## Scope
In scope: the loopback bridge (`hermes_3d_lib/hub.py`), the handling of the link key and runtime files,
anything that runs a subprocess, the scan's read-only guarantee, the Desktop front and dashboard routes.

Out of scope: the Numinia Engine itself (report to its project), Hermes Agent itself, attacks that already
require code execution as your user or write access to `<HERMES_HOME>`, denial of service by a user who
can already stop the processes, and anything needing a non-loopback network position (the plugin never
listens elsewhere).

See [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) for what is defended and by which test.
