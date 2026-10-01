#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg python3 python3-pip python3-venv ca-certificates

WORKER_DIR="${TORRENT_WORKER_DIR:-/opt/torrent-studio-worker}"
MODEL_LOG="${TORRENT_MODEL_LOG:-/var/log/torrent-model.log}"
mkdir -p "$(dirname "$MODEL_LOG")" /workspace/torrent-serverless-jobs
touch "$MODEL_LOG"

python3 -m venv "$WORKER_DIR/.venv"
"$WORKER_DIR/.venv/bin/pip" install --no-cache-dir -r "$WORKER_DIR/requirements.txt"

nohup "$WORKER_DIR/.venv/bin/python" "$WORKER_DIR/worker.py" >>/var/log/torrent-pyworker.log 2>&1 &
nohup "$WORKER_DIR/.venv/bin/python" "$WORKER_DIR/app.py" >>"$MODEL_LOG" 2>&1 &

for _ in $(seq 1 60); do
    curl -fsS http://127.0.0.1:${TORRENT_MODEL_PORT:-8080}/health >/dev/null && exit 0
    sleep 1
done

echo "Torrent encoder fatal: model server did not become healthy" >>"$MODEL_LOG"
exit 1
