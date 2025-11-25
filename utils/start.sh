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

pid_python=
pid_socat=
INTERNAL_PORT=9224
CHROME_PORT=${CHROME_PORT:-9225}

echo "Starting Python application..."
# Ensure your python script passes the INTERNAL_PORT to Chrome
python src/main.py & pid_python=$!


echo "Starting socat port forwarder on port $CHROME_PORT -> localhost:$INTERNAL_PORT"
# TCP-LISTEN: Listen on the external port
# fork: Handle multiple connections
# TCP: Forward to the internal loopback address
socat TCP-LISTEN:"$CHROME_PORT",fork,bind=0.0.0.0 TCP:127.0.0.1:"$INTERNAL_PORT" & pid_socat=$!

wait "$pid_python"

cleanup
exit $?