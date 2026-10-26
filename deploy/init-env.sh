#!/usr/bin/env bash
# Create .env from .env.example with random secrets, and generate the Mosquitto password file.
# Idempotent: existing .env is kept unless --force is given.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f .env && "${1:-}" != "--force" ]]; then
  echo ".env already exists (use --force to regenerate)"; 
else
  rnd() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-24}"; }
  sed -e "s|^SIM_ADMIN_TOKEN=.*|SIM_ADMIN_TOKEN=$(rnd 32)|" \
      -e "s|^MOM_BASIC_PASS=.*|MOM_BASIC_PASS=$(rnd)|" \
      -e "s|^IOT_MQTT_PUB_PASS=.*|IOT_MQTT_PUB_PASS=$(rnd)|" \
      -e "s|^IOT_MQTT_SUB_PASS=.*|IOT_MQTT_SUB_PASS=$(rnd)|" \
      -e "s|^ESB_BASIC_PASS=.*|ESB_BASIC_PASS=$(rnd)|" \
      -e "s|^CV_TARGET_BASIC_PASS=.*|CV_TARGET_BASIC_PASS=__CVPASS__|" \
      -e "s|^CV_SINK_BASIC_PASS=.*|CV_SINK_BASIC_PASS=__CVPASS__|" \
      .env.example | sed -e "s|__CVPASS__|$(rnd)|g" | sed -e 's/[[:space:]]*#.*$//' -e '/^$/d' > .env
  chmod 600 .env
  echo ".env generated"
fi
# mosquitto password file (hashed) from .env values
set -a; source .env; set +a
: > iot/mosquitto/passwd
docker run --rm -v "$ROOT/iot/mosquitto:/m" eclipse-mosquitto:2 sh -c \
  "mosquitto_passwd -b /m/passwd '$IOT_MQTT_PUB_USER' '$IOT_MQTT_PUB_PASS' && mosquitto_passwd -b /m/passwd '$IOT_MQTT_SUB_USER' '$IOT_MQTT_SUB_PASS'"
chown 1883:1883 iot/mosquitto/passwd iot/mosquitto/acl iot/mosquitto/mosquitto.conf && chmod 600 iot/mosquitto/passwd  # mosquitto runs as uid 1883 inside the container
echo "mosquitto passwd generated for $IOT_MQTT_PUB_USER / $IOT_MQTT_SUB_USER"
