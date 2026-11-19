# ADR 001 — ROS 2 rather than ROS 1

**Date:** 2026-04-18  
**Status:** Accepted

## Context

A robotics middleware is needed to drive a NAO and, eventually, drones.
Two options: ROS 1 (Noetic) and ROS 2 (Jazzy).

## Decision

Use **ROS 2 Jazzy Jalisco**.

## Reasons

- **ROS 1 end of life**: ROS Noetic reaches its end of life in May 2025. No security fixes after that date.
- **Native multi-robot**: ROS 2 uses decentralised DDS (Data Distribution Service) — no single `roscore`, no SPOF.
- **Security**: DDS supports authentication and encryption natively (SROS 2).
- **Drones**: PX4, DJI and Parrot all have official ROS 2 drivers. No migration needed to extend the stack.
- **Docker-friendly**: the official `ros:jazzy-ros-base` image is maintained and lightweight.

## Consequences

- The NAO bridge uses `rosbridge_suite` to translate WebSocket JSON ↔ ROS 2 topics, which avoids having NAOqi as a direct dependency in the VPS containers.
- No ROS 1 compatibility required; no `ros1_bridge` in the stack.

---

← [All decisions](../../README.md#architecture-decisions-adr) · [Architecture](../architecture.md)
