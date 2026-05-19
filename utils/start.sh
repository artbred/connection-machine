#!/bin/bash
set -o pipefail -o errexit -o nounset

cleanup () {
  echo "Cleaning up..."
  if [ -n "$pid_python" ]; then
    kill -TERM $pid_python 2>/dev/null || true
  fi
}
trap cleanup TERM INT

pid_python=

echo "Starting Python application..."
python src/main.py & pid_python=$!

wait "$pid_python"

cleanup
exit $?
