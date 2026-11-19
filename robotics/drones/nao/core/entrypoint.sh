#!/usr/bin/env bash
# Starts the ROS2 nodes in parallel: naoqi-driver, cognitive-renderer,
# motion-engine (if MOTION_ENGINE=1), static-driver (static drone) unless DRONE_ID is empty, and
# a second static-driver for the OBS voice if OBS_DRONE_ID is set (ADR 005).
# If one stops, the container stops → Docker restarts it whole.
# docker stop (SIGTERM) is relayed to all nodes, then we wait for them to end.
set -euo pipefail

# setup.bash reads undefined variables (AMENT_TRACE_SETUP_FILES…):
# incompatible with "set -u" on the current ros:humble-ros-base image.
set +u
source /opt/ros/humble/setup.bash
set -u

echo "[nao-core] starting the nodes…"

PIDS=()

python3 -u /ros2_ws/naoqi_driver_node.py      &  PID_DRIVER=$!;   PIDS+=($!)
python3 -u /ros2_ws/cognitive_renderer_node.py &  PID_RENDERER=$!; PIDS+=($!)
echo "[nao-core] naoqi-driver(${PID_DRIVER})  cognitive-renderer(${PID_RENDERER})"

# motion-engine sends free angles at 20 Hz: off in the constrained motor
# universe (profile calibrated from the control room). MOTION_ENGINE=1 brings it back.
if [ "${MOTION_ENGINE:-0}" = "1" ]; then
    python3 -u -m motion_engine.node           &  PID_MOTION=$!; PIDS+=($!)
    echo "[nao-core] motion-engine(${PID_MOTION})"
else
    echo "[nao-core] motion-engine off (MOTION_ENGINE=1 to start it)"
fi

if [ -n "${DRONE_ID:-}" ]; then
    python3 -u /ros2_ws/static_driver_node.py  &  PID_STATIC=$!; PIDS+=($!)
    echo "[nao-core] static-driver(${PID_STATIC}) for ${DRONE_ID}"
else
    echo "[nao-core] DRONE_ID empty — static-driver not started"
fi

# Stream voice (ADR 005): same driver, another body (edge-tts synthesis → OBS).
if [ -n "${OBS_DRONE_ID:-}" ]; then
    DRONE_ID="$OBS_DRONE_ID" python3 -u /ros2_ws/static_driver_node.py  &  PID_OBS=$!; PIDS+=($!)
    echo "[nao-core] static-driver(${PID_OBS}) for ${OBS_DRONE_ID} (OBS voice)"
fi

# bash is the container's PID 1: without a trap, the kernel ignores SIGTERM and
# docker stop waits for its timeout before SIGKILL, without the nodes being told.
stop() {
    echo "[nao-core] stop requested — SIGTERM to the nodes (${PIDS[*]})"
    kill -TERM "${PIDS[@]}" 2>/dev/null || true
    wait
    exit 0
}
trap stop TERM INT

# Exit as soon as a node stops (Docker restarts the whole container)
wait -n
echo "[nao-core] a node stopped — shutting the container down"
exit 1
