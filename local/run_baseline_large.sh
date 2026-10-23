#!/usr/bin/env bash
# Run all-false baseline on large dataset, save to research_results/ablation_results_large/feat_baseline/
# Runs BIRD and Ambrosia separately to avoid checkpoint contamination.
#
# Usage:
#   bash local/run_baseline_large.sh [bird|ambrosia|both]

set -euo pipefail

BENCHMARK="${1:-both}"
OUT_DIR="research_results/ablation_results_large/feat_baseline"
mkdir -p "$OUT_DIR"

run_bird() {
    if [[ -f "$OUT_DIR/bird_query_results.json" ]]; then
        echo "[SKIP] BIRD baseline already done"
        return
    fi
    echo "[BASELINE] Running BIRD large..."
    rm -f query_results.json
    DATASET_SIZE=large bash local/run_single.sh baseline bird
    SRC="research_results/ablation_results/feat_00_baseline_bird"
    [[ -f "$SRC/bird.log" ]]               && cp "$SRC/bird.log"               "$OUT_DIR/"
    [[ -f "$SRC/bird_query_results.json" ]] && cp "$SRC/bird_query_results.json" "$OUT_DIR/"
    rm -rf "$SRC"
    echo "[OK] BIRD baseline saved"
}

run_ambrosia() {
    if [[ -f "$OUT_DIR/ambrosia_query_results.json" ]]; then
        echo "[SKIP] Ambrosia baseline already done"
        return
    fi
    echo "[BASELINE] Running Ambrosia large..."
    rm -f query_results.json
    DATASET_SIZE=large bash local/run_single.sh baseline ambrosia
    SRC="research_results/ablation_results/feat_00_baseline_ambrosia"
    [[ -f "$SRC/ambrosia.log" ]]               && cp "$SRC/ambrosia.log"               "$OUT_DIR/"
    [[ -f "$SRC/ambrosia_query_results.json" ]] && cp "$SRC/ambrosia_query_results.json" "$OUT_DIR/"
    rm -rf "$SRC"
    echo "[OK] Ambrosia baseline saved"
}

case "$BENCHMARK" in
    bird)     run_bird ;;
    ambrosia) run_ambrosia ;;
    both)     run_bird; run_ambrosia ;;
esac

# Write summary
bird_acc=$(grep -oP "Overall accuracy:\s*\K[\d.]+" "$OUT_DIR/bird.log"     2>/dev/null | tail -1 || echo "N/A")
amb_acc=$(grep  -oP "Accuracy:\s*\K[\d.]+"         "$OUT_DIR/ambrosia.log" 2>/dev/null | tail -1 || echo "N/A")
{
    echo "FEAT: baseline (all-false)  benchmark=${BENCHMARK}"
    echo "BIRD:     ${bird_acc}%"
    echo "Ambrosia: ${amb_acc}%"
} | tee "$OUT_DIR/summary.txt"
