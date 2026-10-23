#!/usr/bin/env bash
# Supervise a new tunnel, or watch an existing one and take over if its port closes.
set -uo pipefail
SSH_USER="onik110"
SSH_HOST="lnsigo.mipt.ru"
SSH_PORT="2278"
SSH_PID=""
cleanup() {
    if [[ -n "$SSH_PID" ]]; then
        kill "$SSH_PID" 2>/dev/null || true
        wait "$SSH_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
port_open() { (echo >/dev/tcp/127.0.0.1/5444) 2>/dev/null; }
while true; do
    if [[ -z "$SSH_PID" ]] && port_open; then
        echo "[TUNNEL] Watching existing port 5444 (external process will not be killed)."
        while port_open; do sleep 3; done
        echo "[WATCHDOG] Existing tunnel port closed; starting a managed tunnel."
    fi
    echo "[TUNNEL] Connecting to $SSH_HOST:$SSH_PORT..."
    ssh -N -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o ConnectTimeout=10 -o ConnectionAttempts=1 \
        -L 127.0.0.1:5444:10.11.1.6:5444 \
        -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" &
    SSH_PID=$!
    wait "$SSH_PID"
    status=$?
    SSH_PID=""
    echo "[WATCHDOG] SSH exited ($status); retrying in 3 seconds."
    sleep 3
done
