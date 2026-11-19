# ADR 002 — WebSocket bridge embedded on NAO (no VPN, no Raspberry Pi)

**Date:** 2026-04-18  
**Status:** Accepted

## Context

The NAOqi protocol (port 9559, proprietary binary) does not cross the Internet natively.
A way to drive NAO from the VPS is needed.

Three approaches were considered:

| Option | Description | Rejection |
|--------|-------------|-------|
| VPN (WireGuard/OpenVPN) | L3 tunnel between NAO and VPS | NAO v5/v6: limited network configuration, OpenVPN unstable on embedded Gentoo |
| Intermediate Raspberry Pi | RPi at the edge, does the bridging | Extra hardware, physical SPOF, +50 ms latency |
| **WebSocket script on NAO** | Embedded Python 2.7, outbound connection to the VPS | ✅ Chosen |

## Decision

Embed **`nao_ws_bridge.py`** directly on NAO.

The script:
1. Connects to the local NAOqi daemon (`localhost:9559`) through the `qi` SDK
2. Opens an **outbound** WebSocket connection to `rosbridge_suite` on the VPS (port 9090)
3. Subscribes to the `/nao/*` topics and translates the JSON messages into NAOqi calls

## Reasons

- **Outbound connection**: NAO initiates the connection — no inbound firewall rule needed on the robot.
- **Zero hardware**: no RPi, no intermediate router.
- **Standard protocol**: rosbridge WebSocket + JSON is simple, debuggable with `wscat`, no binary dependency on the VPS side.
- **Resilience**: the script implements automatic reconnection with exponential backoff.

## Constraints

- NAO v5/v6 runs Python 2.7 — the script is written to be Python 2.7 compatible.
- `websocket-client==0.59.0` is the last version supporting Python 2.7.
- The VPS port 9090 must be publicly reachable (`ufw allow 9090` rule).

## Consequences

- The VPS does not need to know NAO's IP — NAO is the one connecting.
- If NAO is off the network, commands pile up in the rosbridge queue and are delivered on reconnection (depending on the QoS config).
- Extensible to drones: same pattern, a different script on the drone side.

---

← [All decisions](../../README.md#architecture-decisions-adr) · [NAO robot](../nao.md)
