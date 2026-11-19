# Static drone

Fixed body (Ubuntu PC or Raspberry Pi 5) placed in the room: microphones, camera,
speaker ([ADR 004](../../../docs/decisions/004-drone-statique.md)).
It connects outbound to the same brain (`robotics/brain/`) as the other drones,
through rosbridge.

- [`onboard/`](onboard/README.md) — onboard program, step-by-step installation
  ([on an Ubuntu laptop](onboard/pc-portable-ubuntu.md)).
- [`driver/`](driver/static_driver_node.py) — ROS 2 node on the VPS side (`nao-core`
  container): `/drones/{id}/*` ↔ `/bodies/{id}/*`, Whisper and edge-tts
  ([details](../../../docs/bridge-ia-ros2-nao.md#static_driver)).
- Test without hardware: `python3 tests/fake-static-drone.py` from the repository
  root, on the VPS ([link 9](../../../docs/test-chain.md#link-9--simulated-static-drone-fake-rosbridge-messages)).
