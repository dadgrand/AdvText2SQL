#!/usr/bin/env bash
# Run all 33 features in isolation on Ambrosia large dataset (240 questions).
# Output: research_results/ablation_results_large/feat_N_ambrosia/ for each feature.
# Skips a feature if research_results/ablation_results_large/feat_N_ambrosia/summary.txt already exists.
#
# Usage:
#   bash local/ablation_large_ambrosia.sh
#
# Estimated time: ~3h per feature × 33 = ~4 days total.
# Safe to stop and restart — completed runs are skipped automatically.

set -uo pipefail

FEATURES=(
    FEAT_1 FEAT_2 FEAT_3 FEAT_4 FEAT_5 FEAT_6 FEAT_7 FEAT_8 FEAT_9
    FEAT_10 FEAT_11 FEAT_12 FEAT_13 FEAT_14 FEAT_15 FEAT_16 FEAT_17
    FEAT_18 FEAT_19 FEAT_20 FEAT_21 FEAT_23 FEAT_25 FEAT_26 FEAT_27
    FEAT_28 FEAT_29 FEAT_30 FEAT_32 FEAT_33 FEAT_34 FEAT_35 FEAT_36
)

OUT_ROOT="research_results/ablation_results_large"
mkdir -p "$OUT_ROOT"

# ── One SSH tunnel for all runs ───────────────────────────────────────────────
SSH_USER="onik110"
SSH_HOST="lnsigo.mipt.ru"
SSH_PORT="2278"
CONTROL_SOCKET="/tmp/ssh_ablation_amb_$$"
SSH_MASTER_PID=""

WATCHDOG_PID=""

start_own_tunnel() {
    ssh -N -M -S "$CONTROL_SOCKET" \
        -o ControlPersist=yes \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o ConnectTimeout=15 \
        -L 5444:10.11.1.6:5444 \
        -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" &
    SSH_MASTER_PID=$!
    for i in $(seq 1 30); do
        if (echo >/dev/tcp/localhost/5444) 2>/dev/null; then
            echo "[TUNNEL] Ready after ${i}s"
            return
        fi
        sleep 1
    done
    echo "[TUNNEL] WARNING: not reachable after 30s"
}

if (echo >/dev/tcp/localhost/5444) 2>/dev/null; then
    echo "[TUNNEL] Already up (e.g. via local/start_tunnel.sh) — reusing, no password needed"
    echo "[TUNNEL] NOTE: that external tunnel needs its own watchdog (start_tunnel.sh has one)"
else
    echo "Connecting SSH tunnel (enter password once)..."
    start_own_tunnel

    # Watchdog: reconnect if the tunnel drops mid-run (Ambrosia builds ~90+ DBs per run)
    ( while true; do
        sleep 30
        if ! (echo >/dev/tcp/localhost/5444) 2>/dev/null; then
            echo "[WATCHDOG] Tunnel down — reconnecting..."
            start_own_tunnel
        fi
    done ) &
    WATCHDOG_PID=$!

    export SSH_CONTROL_SOCKET="$CONTROL_SOCKET"
    trap "kill $WATCHDOG_PID 2>/dev/null; [[ -n '${SSH_MASTER_PID:-}' ]] && { ssh -S '$CONTROL_SOCKET' -O exit '$SSH_USER@$SSH_HOST' 2>/dev/null; kill \$SSH_MASTER_PID 2>/dev/null; }" EXIT
fi

TOTAL=${#FEATURES[@]}
DONE=0
SKIPPED=0

echo "========================================"
echo " Ablation large Ambrosia — $TOTAL features"
echo " Output root: $OUT_ROOT/"
echo "========================================"

for FEAT in "${FEATURES[@]}"; do
    OUT_DIR="$OUT_ROOT/${FEAT,,}_ambrosia"

    if [[ -f "$OUT_DIR/summary.txt" ]]; then
        echo "[SKIP] $FEAT — already done"
        (( SKIPPED++ )) || true
        continue
    fi

    echo ""
    echo "[$((DONE + SKIPPED + 1))/$TOTAL] Starting $FEAT ..."

    # Keep checkpoint if resuming the same interrupted feature, else clear it
    MARKER=".ablation_current_feat_ambrosia"
    if [[ -f "$MARKER" ]] && [[ "$(cat "$MARKER")" == "$FEAT" ]] && [[ -s query_results.json ]]; then
        echo "  [resuming $FEAT] checkpoint сохранён"
    else
        rm -f query_results.json
        echo "  [fresh] checkpoint очищен"
    fi
    echo "$FEAT" > "$MARKER"

    # run_single.sh writes to research_results/ablation_results/ by default — move result after
    DATASET_SIZE=large bash local/run_single.sh "$FEAT" ambrosia

    SRC="research_results/ablation_results/${FEAT,,}_ambrosia"
    if [[ -d "$SRC" ]]; then
        rm -rf "$OUT_DIR"
        mv "$SRC" "$OUT_DIR"
        echo "[OK] $FEAT → $OUT_DIR — $(grep 'Ambrosia:' "$OUT_DIR/summary.txt" 2>/dev/null || echo 'see log')"
    else
        echo "[WARN] Expected $SRC not found — check run_single.sh output"
    fi
    (( DONE++ )) || true
done

echo ""
echo "========================================"
echo " Done: $DONE new runs, $SKIPPED skipped"
echo "========================================"
echo ""
echo "Next step — build feature dataset:"
echo "  python scripts/build_feature_dataset.py \\"
echo "    --large --benchmark ambrosia \\"
echo "    --ablation-dir $OUT_ROOT \\"
echo "    --data-file data/ambrosia_large.json \\"
echo "    --out data/feature_labels_ambrosia_large.json"
