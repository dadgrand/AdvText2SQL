#!/usr/bin/env bash
# Run from the repository root in Git Bash. No separate SSH terminal is needed.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
TUNNEL_PID=""
cleanup() {
    if [[ -n "$TUNNEL_PID" ]]; then
        kill "$TUNNEL_PID" 2>/dev/null || true
        wait "$TUNNEL_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
mkdir -p research_results/runs
echo "[TUNNEL] Connection log: research_results/runs/baseline_tunnel.log"
# Supervision also runs when reusing an existing tunnel.
bash local/start_tunnel.sh >> research_results/runs/baseline_tunnel.log 2>&1 &
TUNNEL_PID=$!
if ! (echo >/dev/tcp/127.0.0.1/5444) 2>/dev/null; then
    ready=false
    for ((i=0; i<45; i++)); do
        if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
            echo "SSH tunnel failed; baseline was not started." >&2
            exit 1
        fi
        if (echo >/dev/tcp/127.0.0.1/5444) 2>/dev/null; then
            ready=true
            break
        fi
        sleep 1
    done
    if [[ "$ready" != true ]]; then
        echo "SSH tunnel not ready; baseline was not started." >&2
        exit 1
    fi
fi
if [[ -x .venv/Scripts/python.exe ]]; then
    PYTHON=.venv/Scripts/python.exe
elif [[ -x .venv/bin/python ]]; then
    PYTHON=.venv/bin/python
else
    PYTHON=python
fi
"$PYTHON" -u scripts/run_openrouter_baseline.py "$@"
