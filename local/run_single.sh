#!/usr/bin/env bash
# Run one or more feature flags on a benchmark, saving to research_results/ablation_results/.
#
# Usage: bash local/run_single.sh FEAT_1 ambrosia
#        bash local/run_single.sh FEAT_5 bird
#        bash local/run_single.sh "FEAT_5 FEAT_32 FEAT_33 FEAT_34" both
#
# First arg: space-separated list of flags to enable (all others forced false)
# Benchmark options: ambrosia | bird | both

set -euo pipefail

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

FEAT_FLAGS="${1:-}"
BENCHMARK="${2:-ambrosia}"

if [[ -z "$FEAT_FLAGS" ]]; then
    echo "Usage: bash local/run_single.sh \"FEAT_N [FEAT_M ...]\" [ambrosia|bird|both]"
    echo "       bash local/run_single.sh baseline [ambrosia|bird|both]"
    exit 1
fi

# Special keyword: baseline = all flags false
if [[ "$FEAT_FLAGS" == "baseline" ]]; then
    FEAT_FLAGS=""
    FEAT_FLAG="feat_00_baseline"
else
    FEAT_FLAG="${FEAT_FLAGS%% *}"
fi

# ── SSH tunnel ────────────────────────────────────────────────────────────────
SSH_USER="onik110"
SSH_HOST="lnsigo.mipt.ru"
SSH_PORT="2278"
TUNNEL_SPEC="5444:10.11.1.6:5444"
TUNNEL_PID=""

start_tunnel() {
    echo "[TUNNEL] Starting SSH tunnel..."
    ssh -N \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o ConnectTimeout=10 \
        -o ExitOnForwardFailure=yes \
        -L "$TUNNEL_SPEC" \
        -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" &
    TUNNEL_PID=$!
    # Wait until port 5444 is reachable (up to 30s)
    for i in $(seq 1 30); do
        if (echo >/dev/tcp/localhost/5444) 2>/dev/null; then
            echo "[TUNNEL] Ready after ${i}s (PID=$TUNNEL_PID)"
            return
        fi
        sleep 1
    done
    echo "[TUNNEL] WARNING: port 5444 not reachable after 30s — proceeding anyway"
}

check_tunnel() {
    (echo >/dev/tcp/localhost/5444) 2>/dev/null
}

watchdog_loop() {
    while true; do
        sleep 30
        if ! check_tunnel; then
            echo "[WATCHDOG] Tunnel down — restarting..."
            start_tunnel
        fi
    done
}

cleanup() {
    [[ -n "${WATCHDOG_PID:-}" ]] && kill "$WATCHDOG_PID" 2>/dev/null || true
    [[ -n "${TUNNEL_PID:-}"   ]] && kill "$TUNNEL_PID"   2>/dev/null || true
    rm -f "$TMP_ENV"
}
trap cleanup EXIT

if check_tunnel; then
    echo "[TUNNEL] Already up — reusing existing connection"
    TUNNEL_PID=""
    WATCHDOG_PID=""
else
    start_tunnel
    watchdog_loop &
    WATCHDOG_PID=$!
fi

# ── uv ───────────────────────────────────────────────────────────────────────
export PATH="/c/Users/718/anaconda3/Scripts:/mnt/c/Users/718/anaconda3/Scripts:$PATH"

UV=$(command -v uv 2>/dev/null || \
     ls /c/Users/718/anaconda3/Scripts/uv* 2>/dev/null | head -1 || \
     ls /mnt/c/Users/718/anaconda3/Scripts/uv* 2>/dev/null | head -1) || true

if [[ -z "$UV" ]]; then
    echo "ERROR: uv not found."
    exit 1
fi

unset FEAT_1 FEAT_2 FEAT_3 FEAT_4 FEAT_5 FEAT_6 FEAT_7 FEAT_8 FEAT_9
unset FEAT_10 FEAT_11 FEAT_12 FEAT_13 FEAT_14 FEAT_15 FEAT_16 FEAT_17 FEAT_18 FEAT_19
unset FEAT_20 FEAT_21 FEAT_22 FEAT_23 FEAT_25 FEAT_26 FEAT_27 FEAT_28 FEAT_29 FEAT_30 FEAT_32 FEAT_33 FEAT_34 FEAT_35 FEAT_36
unset OPTIMISTIC_AMBIGUITY_FALLBACK

# ── Build isolated tmp env ────────────────────────────────────────────────────
TMP_ENV=$(mktemp -p . .run_single_tmp.XXXXXX)

grep -v "^FEAT_[0-9]\|^LLM_BASE_INTERVAL\|^MAX_RETRIES\|^DATASET_SIZE" .env > "$TMP_ENV"
printf 'FEAT_1=false\nFEAT_2=false\nFEAT_3=false\nFEAT_4=false\n'             >> "$TMP_ENV"
printf 'FEAT_5=false\nFEAT_6=false\nFEAT_7=false\nFEAT_8=false\nFEAT_9=false\n' >> "$TMP_ENV"
printf 'FEAT_10=false\nFEAT_11=false\nFEAT_12=false\nFEAT_13=false\n'         >> "$TMP_ENV"
printf 'FEAT_14=false\nFEAT_15=false\nFEAT_16=false\nFEAT_17=false\n'         >> "$TMP_ENV"
printf 'FEAT_18=false\nFEAT_19=false\nFEAT_20=false\n'                        >> "$TMP_ENV"
printf 'FEAT_21=false\nFEAT_22=false\nFEAT_23=false\nFEAT_25=false\n'         >> "$TMP_ENV"
printf 'FEAT_26=false\nFEAT_27=false\nFEAT_28=false\nFEAT_29=false\n'         >> "$TMP_ENV"
printf 'FEAT_32=false\nFEAT_33=false\nFEAT_34=false\nFEAT_35=false\n'         >> "$TMP_ENV"
printf 'FEAT_36=false\nFEAT_30=false\n'                                         >> "$TMP_ENV"
printf 'OPTIMISTIC_AMBIGUITY_FALLBACK=false\n'                                 >> "$TMP_ENV"
printf "PYTHONIOENCODING=utf-8\nPYTHONUTF8=1\nLLM_BASE_INTERVAL=${LLM_BASE_INTERVAL:-15}\nMAX_RETRIES=${MAX_RETRIES:-5}\n" >> "$TMP_ENV"
printf "DATASET_SIZE=${DATASET_SIZE:-large}\n"                                 >> "$TMP_ENV"

# Enable all specified flags
for flag in $FEAT_FLAGS; do
    sed -i "s/^${flag}=.*/${flag}=true/" "$TMP_ENV"
done

echo ""
echo "========================================"
echo " RUN: ${FEAT_FLAGS}  benchmark=${BENCHMARK}"
echo " Active flags:"
grep "^FEAT_" "$TMP_ENV"
echo "========================================"

# ── Output dir ───────────────────────────────────────────────────────────────
OUT_DIR="research_results/ablation_results/${FEAT_FLAG,,}_${BENCHMARK}"
mkdir -p "$OUT_DIR"
echo "Logs → $OUT_DIR/"

# ── Run ───────────────────────────────────────────────────────────────────────
if [[ "$BENCHMARK" == "ambrosia" || "$BENCHMARK" == "both" ]]; then
    echo "--- AMBROSIA ---"
    "$UV" run --env-file "$TMP_ENV" ambrosia_benchmark.py 2>&1 \
        | tee "$OUT_DIR/ambrosia.log"
    [[ -f query_results.json ]] && cp query_results.json "$OUT_DIR/ambrosia_query_results.json"
    rm -f query_results.json
fi

if [[ "$BENCHMARK" == "bird" || "$BENCHMARK" == "both" ]]; then
    echo "--- BIRD ---"
    "$UV" run --env-file "$TMP_ENV" bird_benchmark.py 2>&1 \
        | tee "$OUT_DIR/bird.log"
    [[ -f query_results.json ]] && cp query_results.json "$OUT_DIR/bird_query_results.json"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
bird_acc=$(grep -oP "Overall accuracy:\s*\K[\d.]+" "$OUT_DIR/bird.log"     2>/dev/null | tail -1 || echo "N/A")
amb_acc=$(grep  -oP "Accuracy:\s*\K[\d.]+"         "$OUT_DIR/ambrosia.log" 2>/dev/null | tail -1 || echo "N/A")
{
    echo "FEAT: ${FEAT_FLAGS}  benchmark=${BENCHMARK}"
    echo "BIRD:     ${bird_acc}%"
    echo "Ambrosia: ${amb_acc}%"
} | tee "$OUT_DIR/summary.txt"
