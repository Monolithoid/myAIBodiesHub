# NAO relay on the static drone

The NAO no longer talks to the VPS directly: it sends its requests to the static
drone, on the local network, and the drone relays them over TLS
([ADR 006](../../../../../docs/decisions/006-nao-via-drone-statique.md)).

```
NAO (Wi-Fi) ── ws://, no token ──► drone :9090 (Caddy)
                                        │ + Authorization: Bearer $ROSBRIDGE_TOKEN
                                        └─► wss://rosbridge.$DOMAIN (TLS) ─► VPS
```

Why: the NAO's Python 2.7 cannot do TLS. Without a relay, the VPS had to accept
rosbridge over plain HTTP (port 80), with the token visible on the Internet. With the
relay, the VPS only accepts `wss://`, and the token is no longer stored on the robot.
The local network is considered safe.

| File | Role |
|---|---|
| `Caddyfile` | The relay: WebSocket reverse proxy, adds the token |
| `nao-relay.service` | systemd service for the relay |
| `setup-ubuntu.sh` | Automatic installation on Ubuntu (idempotent) |
| `run.sh` | Starts the relay by hand, without a service |

The commands apply to the Ubuntu PC as well as the Raspberry Pi. The drone must
already be working ([drone installation](../README.md)).

## Automatic installation

On Ubuntu, `setup-ubuntu.sh` does steps 2 and 3 and checks the relay (step 5 table).
It is idempotent.

```bash
bash ~/ia-agent/robotics/drones/static/onboard/nao-relay/setup-ubuntu.sh
bash ~/ia-agent/robotics/drones/static/onboard/nao-relay/run.sh   # starts the relay, Ctrl+C to stop it
```

By default, the relay does not start on its own: it is started with `run.sh` when the
NAO needs to reach the VPS. On a dedicated PC, `setup-ubuntu.sh --service` installs
the `nao-relay` service, started at boot; rerunning the script only restarts it if
`.env`, the `Caddyfile` or the service changed.

Run it as the drone user, **without sudo**, after installing the drone
(`../setup-ubuntu.sh`): it reads `ROSBRIDGE_TOKEN` from the drone's `.env` and
suggests `NAO_RELAY_UPSTREAM` based on `ROSBRIDGE_URL`. At the end, it shows the
drone's address and the line to put in the NAO's `autostart.sh`. Still to do by hand:
the DHCP reservation (step 1) and the NAO (step 4).

## 1. A fixed address for the drone

The NAO must find the drone at the same address on every start. The simplest: a
**DHCP reservation** for the drone in the router's interface. Then note its address:

```bash
hostname -I          # e.g. 192.168.1.20
```

## 2. Install Caddy

```bash
sudo apt update
sudo apt install -y caddy
# Caddy starts with a default config: stop it, nao-relay replaces it.
sudo systemctl disable --now caddy
```

`caddy` is in the Ubuntu 24.04 and Raspberry Pi OS Bookworm repositories. On Ubuntu
22.04, first add the official repository:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## 3. Configure and start the relay

In the drone's `.env` (`../.env`, see `../.env.example`):

```bash
NAO_RELAY_UPSTREAM=https://rosbridge.mydomain.com   # same host as ROSBRIDGE_URL, with https://
ROSBRIDGE_TOKEN=...                                 # already present for the drone
```

By default the relay accepts the whole local network (192.168.x, 172.16-31.x, 10.x)
and refuses the rest. To accept only the NAO: `NAO_IP=192.168.1.42`.

Adapt `User` and the paths in `nao-relay.service`, then:

```bash
sudo cp nao-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nao-relay
journalctl -u nao-relay -f
```

If the drone's firewall is active (`sudo ufw status`), open the port to the local
network: `sudo ufw allow from 192.168.0.0/16 to any port 9090 proto tcp`.

## 4. NAO side

In `/home/nao/autostart.sh`, the URL points to the drone and the token goes away:

```bash
#!/bin/bash
sleep 10   # wait for network
python /home/nao/nao_ws_bridge.py --vps ws://192.168.1.20:9090/ --provision &
```

Replace `192.168.1.20` with the drone's address, remove the
`export ROSBRIDGE_TOKEN=...` line, then restart the bridge (or the NAO). The bridge
log must show `[bridge] Connected.` then `[bridge] Ready.`

## 5. Checks

| Test | Where | Expected |
|---|---|---|
| `journalctl -u nao-relay -f` | drone | a `status: 101` line on every NAO connection |
| `ros2 topic echo /nao/status` | VPS | the NAO's battery every 30 s |
| `curl -i http://rosbridge.mydomain.com/` | anywhere | no more plain-HTTP rosbridge (404 or redirect) |

If the NAO cannot connect, try from another computer on the local network:

```bash
curl -i -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  http://192.168.1.20:9090/
```

`101 Switching Protocols`: the relay and the VPS answer. `403`: the address is not
accepted (see `NAO_IP`). `401`: token refused by the VPS (the drone's
`ROSBRIDGE_TOKEN` differs from the VPS one). `502`: the drone cannot reach
`NAO_RELAY_UPSTREAM`.
