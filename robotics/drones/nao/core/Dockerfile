FROM ros:humble-ros-base

# Build context: robotics/ (to include robotics/shared/)
# Brings naoqi-driver and cognitive-renderer together in one container,
# plus static-driver, the static drone's driver (ADR 004): same dependencies
# (Whisper, edge-tts), same ROS 2 network.

ENV DEBIAN_FRONTEND=noninteractive \
    ROS_DOMAIN_ID=42 \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    PYTHONUNBUFFERED=1 \
    ROBOT_ID=nao_01 \
    DRONE_ID=static_01 \
    NAOQI_BRIDGE_URL=http://localhost:8090

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        python3-numpy \
        ros-humble-std-msgs \
        ros-humble-sensor-msgs \
        ros-humble-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*

# pip dependencies (naoqi-driver and static-driver: requests + edge-tts)
COPY drones/nao/driver/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /ros2_ws

# Scripts of the three nodes and shared module (Whisper + edge-tts)
COPY shared/speech_services.py                               ./speech_services.py
COPY drones/nao/driver/naoqi_driver_node.py                  ./naoqi_driver_node.py
COPY drones/nao/driver/motion_policy.py                      ./motion_policy.py
COPY drones/nao/driver/led_sync.py                           ./led_sync.py
COPY drones/nao/cognitive_renderer/cognitive_renderer_node.py ./cognitive_renderer_node.py
COPY drones/nao/cognitive_renderer/speaking_animation.py      ./speaking_animation.py
COPY drones/static/driver/static_driver_node.py              ./static_driver_node.py

# Entrypoint
COPY drones/nao/core/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

CMD ["./entrypoint.sh"]
