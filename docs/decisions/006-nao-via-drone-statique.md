# ADR 006 — NAO goes through the static drone to reach the VPS

**Date:** 2026-10-09  
**Status:** Accepted

## Context

Until now the NAO connected to the VPS on its own ([ADR 002](002-nao-websocket-bridge.md)):

```
NAO ── plain ws://, port 80 ──► Traefik (rosbridge-ws router, http entrypoint)
        Authorization: Bearer token      └─► rosbridge-proxy (Caddy, checks the token)
                                               └─► ros2-bridge :9090
```

`nao_ws_bridge.py` can only do `ws://`: OpenNAO's Python 2.7 has no usable TLS. The
VPS therefore had to keep a Traefik router in **plain HTTP** just for the NAO, and
the `ROSBRIDGE_TOKEN` token crossed the Internet unencrypted. The token was also
stored on the robot.

The static drone ([ADR 004](004-drone-statique.md)) is in the same room, on an
up-to-date machine (Ubuntu PC, then Raspberry Pi 5), and already goes out over
`wss://` with the same token.

**Goal: no more plain HTTP on the VPS.** The local network is considered safe: we
are not trying to isolate the NAO.

## Decision

The NAO stays on Wi-Fi on the local network and sends its requests to a **Caddy
relay on the static drone**, which forwards them to the VPS over TLS and adds the
token.

```
NAO (Wi-Fi) ── ws://, no token ──► drone :9090 (Caddy)
                                        │ + Authorization: Bearer $ROSBRIDGE_TOKEN
                                        └─► wss://rosbridge.$DOMAIN (TLS) ─► VPS
```

- NAO side: `--vps ws://<drone IP>:9090/`, no more `--token`. No code change.
- Drone side: `robotics/drones/static/onboard/nao-relay/` (`Caddyfile` + systemd
  service), configured by the drone's `.env` (`NAO_RELAY_UPSTREAM`,
  `ROSBRIDGE_TOKEN`). The relay only accepts private addresses (or `NAO_IP` if set):
  the token never goes out to the Internet, even if port 9090 ended up exposed.
- VPS side: the Traefik router `rosbridge-ws` (`http` entrypoint) is removed;
  rosbridge is now only reachable over `wss://`.

## Options rejected

| Option | Why |
|---|---|
| TLS on the NAO (embedded `stunnel`) | Depends on `opkg`/OpenSSL on an old firmware; still documented in [nao.md](../nao.md#ssl--wss) for a NAO without a drone |
| ROS 2 / naoqi_driver on the drone | Makes the drone heavier (against ADR 004) and duplicates the embedded bridge code |
| Network isolation of the NAO (cable, VLAN, firewall) | Not the goal: the local network is considered safe |
| Drone ↔ VPS VPN | More complex for the same result; `wss://` already encrypts |

## What is lost

- **NAO without a drone**: it can no longer reach the VPS directly (no more plain
  HTTP). It needs the relay, or `stunnel` on the robot.
- **One more service on the drone** (Caddy), and the drone must keep a fixed address
  on the local network (DHCP reservation).

## What is kept

- The NAO code (`nao_ws_bridge.py`): unchanged.
- The "outbound connection" pattern of ADR 002, on the drone side as on the NAO side.
- The drone stays light: a reverse proxy only relays bytes.

## Consequences

- Amends [ADR 002](002-nao-websocket-bridge.md): an intermediary, rejected then, is
  chosen now that the drone exists.
- **Deployment order**: install the relay and change `autostart.sh` on the NAO
  **before** redeploying the VPS without the HTTP router, otherwise the NAO loses
  the connection in between.

---

← [All decisions](../../README.md#architecture-decisions-adr) · [NAO robot](../nao.md) · [ADR 004](004-drone-statique.md)
