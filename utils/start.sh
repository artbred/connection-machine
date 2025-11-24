#!/bin/bash
set -o pipefail -o errexit -o nounset

cleanup () {
  echo "Cleaning up..."
  if [ -n "$pid_python" ]; then
    kill -TERM $pid_python 2>/dev/null || true
  fi
  if [ -n "$pid_socat" ]; then
    kill -TERM $pid_socat 2>/dev/null || true
  fi
}
trap cleanup TERM INT

wait_for_port() {
  local port=$1
  local max_attempts=${2:-30}
  local attempt=0
  
  echo "Waiting for port $port to become available..."
  while ! nc -z 127.0.0.1 "$port" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ $attempt -ge $max_attempts ]; then
      echo "Timeout waiting for port $port"
      return 1
    fi
    sleep 0.5
  done
  echo "Port $port is now available"
}

pid_python=
pid_socat=
INTERNAL_PORT=9224
CHROME_PORT=${CHROME_PORT:-9225}

echo "Starting Python application..."
# Ensure your python script passes the INTERNAL_PORT to Chrome
python src/main.py & pid_python=$!

# Wait for internal port to be available before starting socat
wait_for_port "$INTERNAL_PORT" || { cleanup; exit 1; }

echo "Starting socat port forwarder on port $CHROME_PORT -> localhost:$INTERNAL_PORT"
# TCP-LISTEN: Listen on the external port
# fork: Handle multiple connections
# TCP: Forward to the internal loopback address
socat TCP-LISTEN:"$CHROME_PORT",fork,bind=0.0.0.0 TCP:127.0.0.1:"$INTERNAL_PORT" & pid_socat=$!

wait "$pid_python"

cleanup
exit $?