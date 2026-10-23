#!/usr/bin/env bash
# Leave-one-in ablation for all implemented FEAT flags.
# Runs BIRD-small + Ambrosia-small per feature in isolation.
#
# Methodology (v2): baseline = ALL flags false (equivalent to main branch behavior).
# Each feature is tested with ONLY that flag enabled (+ intra-feature dependencies).
# No shared "base stack" — code now handles all-false gracefully:
#   - schema: always falls back to light schema even when FEAT_5=false
#   - ambiguity: AMBIGUITY_PROMPT_SIMPLE has core rules ("default=unambiguous")
#   - rate limits: benchmarks always sleep 2s base + FEAT_19 adds extra
#
# Intra-feature dependencies (feature X requires Y to do anything meaningful):
#   FEAT_3/4/7 require FEAT_2 (they process column stats produced by FEAT_2)
#   FEAT_33 requires FEAT_32 (Stage 2a depends on Stage 1 taxonomy)
#   FEAT_34 requires FEAT_32+FEAT_33 (Stage 2b depends on both prior stages)
#
# Usage: bash local/ablation_full.sh (from project root)

set -euo pipefail

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

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
    sleep 3
    echo "[TUNNEL] Started (PID=$TUNNEL_PID)"
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
    echo "[TUNNEL] Cleaning up..."
    kill "$WATCHDOG_PID" 2>/dev/null || true
    kill "$TUNNEL_PID"   2>/dev/null || true
    # remove any leftover tmp envs
    rm -f .ablation_tmp.*
}
trap cleanup EXIT

start_tunnel
watchdog_loop &
WATCHDOG_PID=$!

# ── uv ───────────────────────────────────────────────────────────────────────
export PATH="/c/Users/718/anaconda3/Scripts:/mnt/c/Users/718/anaconda3/Scripts:$PATH"

UV=$(command -v uv 2>/dev/null || \
     ls /c/Users/718/anaconda3/Scripts/uv* 2>/dev/null | head -1 || \
     ls /mnt/c/Users/718/anaconda3/Scripts/uv* 2>/dev/null | head -1) || true

if [[ -z "$UV" ]]; then
    echo "ERROR: uv not found."
    exit 1
fi
echo "Using uv: $UV"

unset FEAT_1 FEAT_2 FEAT_3 FEAT_4 FEAT_5 FEAT_6 FEAT_7 FEAT_8 FEAT_9
unset FEAT_10 FEAT_11 FEAT_12 FEAT_13 FEAT_14 FEAT_15 FEAT_16 FEAT_17 FEAT_18 FEAT_19
unset FEAT_20 FEAT_21 FEAT_22 FEAT_23 FEAT_25 FEAT_26 FEAT_27 FEAT_28 FEAT_29 FEAT_30 FEAT_32 FEAT_33 FEAT_34 FEAT_35 FEAT_36
unset OPTIMISTIC_AMBIGUITY_FALLBACK

RESULTS_DIR="research_results/ablation_results_small"
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

# ── run_feature LABEL DIR_NAME [FEAT_N=true ...] ─────────────────────────────
run_feature() {
    local label="$1" dir_name="$2"; shift 2
    local overrides=("$@")
    local out_dir="$RESULTS_DIR/$dir_name"

    echo ""
    echo "========================================"
    echo " RUN: $label"
    echo " dir: $out_dir"
    echo " overrides: ${overrides[*]:-none}"
    echo "========================================"

    mkdir -p "$out_dir"

    if [[ -f "$out_dir/summary.txt" ]]; then
        echo "[SKIP: summary.txt exists]"
        return
    fi

    # Build isolated env: strip FEAT_* from .env, set all false, override one
    local tmp_env
    tmp_env=$(mktemp -p . .ablation_tmp.XXXXXX)
    grep -v "^FEAT_[0-9]" .env > "$tmp_env"
    printf 'FEAT_1=false\nFEAT_2=false\nFEAT_3=false\nFEAT_4=false\n'             >> "$tmp_env"
    printf 'FEAT_5=false\nFEAT_6=false\nFEAT_7=false\nFEAT_8=false\nFEAT_9=false\n' >> "$tmp_env"
    printf 'FEAT_10=false\nFEAT_11=false\nFEAT_12=false\nFEAT_13=false\n'         >> "$tmp_env"
    printf 'FEAT_14=false\nFEAT_15=false\nFEAT_16=false\nFEAT_17=false\n'         >> "$tmp_env"
    printf 'FEAT_18=false\nFEAT_19=false\nFEAT_20=false\n'                        >> "$tmp_env"
    printf 'FEAT_21=false\nFEAT_22=false\nFEAT_23=false\nFEAT_25=false\n'         >> "$tmp_env"
    printf 'FEAT_26=false\nFEAT_27=false\nFEAT_28=false\nFEAT_29=false\n'         >> "$tmp_env"
    printf 'FEAT_32=false\nFEAT_33=false\nFEAT_34=false\nFEAT_35=false\n'         >> "$tmp_env"
    printf 'FEAT_36=false\nFEAT_30=false\n'                                         >> "$tmp_env"
    printf 'OPTIMISTIC_AMBIGUITY_FALLBACK=false\n'                                 >> "$tmp_env"
    printf 'PYTHONIOENCODING=utf-8\nPYTHONUTF8=1\nLLM_BASE_INTERVAL=20\n'          >> "$tmp_env"

    for f in "${overrides[@]}"; do
        local key="${f%%=*}"
        sed -i "s/^${key}=.*/${f}/" "$tmp_env"
    done

    echo "Active FEAT flags:"
    grep "^FEAT_" "$tmp_env"

    echo "--- BIRD ---"
    "$UV" run --env-file "$tmp_env" bird_benchmark.py \
        2>&1 | tee "$out_dir/bird.log"
    [[ -f query_results.json ]] && cp query_results.json "$out_dir/bird_query_results.json"

    echo "--- AMBROSIA ---"
    "$UV" run --env-file "$tmp_env" ambrosia_benchmark.py \
        2>&1 | tee "$out_dir/ambrosia.log"
    [[ -f query_results.json ]] && cp query_results.json "$out_dir/ambrosia_query_results.json"

    rm -f "$tmp_env"

    local bird_acc amb_acc
    bird_acc=$(grep -oP "Overall accuracy:\s*\K[\d.]+" "$out_dir/bird.log"     2>/dev/null | tail -1 || echo "N/A")
    amb_acc=$(grep  -oP "Accuracy:\s*\K[\d.]+"         "$out_dir/ambrosia.log" 2>/dev/null | tail -1 || echo "N/A")

    cat > "$out_dir/summary.txt" <<EOF
Label:   $label
Commit:  $COMMIT
BIRD:    ${bird_acc}%
Ambrosia:${amb_acc}%
EOF

    echo "Done: BIRD=${bird_acc}%  Ambrosia=${amb_acc}%"
}

# ── Runs ─────────────────────────────────────────────────────────────────────
# Baseline: all FEAT flags false — equivalent to main branch behavior
run_feature "BASELINE: all flags false" "feat_00_baseline"

# Schema features
run_feature "FEAT_1:  FK/PK relationships"        "feat_01_fk_pk"           FEAT_1=true
run_feature "FEAT_5:  light schema"               "feat_05_light_schema"    FEAT_5=true
run_feature "FEAT_6:  heavy schema"               "feat_06_heavy_schema"    FEAT_6=true
run_feature "FEAT_9:  dump db_schemas.json"       "feat_09_schema_dump"     FEAT_9=true
run_feature "FEAT_35: TSV schema format"          "feat_35_tsv"             FEAT_35=true

# Column stats features (FEAT_3/4/7 require FEAT_2 to produce stats first)
run_feature "FEAT_2:  column statistics"          "feat_02_column_stats"    FEAT_2=true
run_feature "FEAT_3:  regex type detection"       "feat_03_regex_types"     FEAT_2=true FEAT_3=true
run_feature "FEAT_4:  pg_stat row count"          "feat_04_pg_stat"         FEAT_2=true FEAT_4=true
run_feature "FEAT_7:  compact stats formatting"   "feat_07_compact_stats"   FEAT_2=true FEAT_7=true

# Infrastructure / observability features
run_feature "FEAT_8:  PG rollback guards"         "feat_08_rollback"        FEAT_8=true
run_feature "FEAT_10: build timing log"           "feat_10_timing"          FEAT_10=true
run_feature "FEAT_11: JSON structured logging"    "feat_11_json_log"        FEAT_11=true

# Ambiguity / reliability features
run_feature "FEAT_12: optimistic ambiguity fallback" "feat_12_optimistic"   FEAT_12=true
run_feature "FEAT_13: few-shot ambiguity prompt"  "feat_13_fewshot"         FEAT_13=true
run_feature "FEAT_18: LLM exponential backoff"    "feat_18_backoff"         FEAT_18=true
run_feature "FEAT_19: throttle sleep"             "feat_19_throttle"        FEAT_19=true

# SQL generation / quality features
run_feature "FEAT_14: strict SQL rules"           "feat_14_strict_sql"      FEAT_14=true
run_feature "FEAT_15: sanitize_sql"               "feat_15_sanitize"        FEAT_15=true
run_feature "FEAT_16: sqlglot validate"           "feat_16_validate"        FEAT_16=true
run_feature "FEAT_17: retry loop"                 "feat_17_retry"           FEAT_17=true
run_feature "FEAT_20: LLM-as-judge verify"        "feat_20_llm_verify"      FEAT_20=true
run_feature "FEAT_27: true self-correction"       "feat_27_self_correction" FEAT_27=true
run_feature "FEAT_29: complexity-based prompts"   "feat_29_complexity"      FEAT_29=true
run_feature "FEAT_36: smart LIMIT injection"      "feat_36_smart_limit"     FEAT_36=true

# AmbiSQL pipeline (FEAT_33 requires FEAT_32; FEAT_34 requires FEAT_32+FEAT_33)
run_feature "FEAT_32: taxonomy detection"         "feat_32_taxonomy"        FEAT_32=true
run_feature "FEAT_33: clarification questions"    "feat_33_clarification"   FEAT_32=true FEAT_33=true
run_feature "FEAT_34: query rewriting"            "feat_34_rewrite"         FEAT_32=true FEAT_33=true FEAT_34=true

# Experimental features
run_feature "FEAT_25: per-question schema pruning" "feat_25_schema_pruning" FEAT_25=true
run_feature "FEAT_26: learnt_hints between runs"  "feat_26_learnt_hints"    FEAT_26=true
run_feature "FEAT_28: self-consistency voting"    "feat_28_self_consistency" FEAT_28=true
run_feature "FEAT_21: MinHash column similarity"  "feat_21_minhash"         FEAT_21=true
run_feature "FEAT_23: NL table descriptions"      "feat_23_nl_desc"         FEAT_23=true
run_feature "FEAT_30: CoT decomposition"          "feat_30_decompose"       FEAT_30=true

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " ABLATION SUMMARY (all features)"
echo "========================================"
printf "%-40s  %-10s  %-10s\n" "Feature" "BIRD%" "Ambrosia%"
printf "%-40s  %-10s  %-10s\n" "-------" "-----" "---------"

for dir_name in \
    feat_00_baseline \
    feat_01_fk_pk \
    feat_02_column_stats \
    feat_03_regex_types \
    feat_04_pg_stat \
    feat_05_light_schema \
    feat_06_heavy_schema \
    feat_07_compact_stats \
    feat_08_rollback \
    feat_09_schema_dump \
    feat_10_timing \
    feat_11_json_log \
    feat_12_optimistic \
    feat_13_fewshot \
    feat_14_strict_sql \
    feat_15_sanitize \
    feat_16_validate \
    feat_17_retry \
    feat_18_backoff \
    feat_19_throttle \
    feat_20_llm_verify \
    feat_25_schema_pruning \
    feat_26_learnt_hints \
    feat_27_self_correction \
    feat_28_self_consistency \
    feat_29_complexity \
    feat_32_taxonomy \
    feat_33_clarification \
    feat_34_rewrite \
    feat_35_tsv \
    feat_36_smart_limit \
    feat_21_minhash \
    feat_23_nl_desc \
    feat_30_decompose; do
    bird_log="$RESULTS_DIR/$dir_name/bird.log"
    amb_log="$RESULTS_DIR/$dir_name/ambrosia.log"
    bird_acc=$(grep -oP "Overall accuracy:\s*\K[\d.]+" "$bird_log" 2>/dev/null | tail -1 || echo "—")
    amb_acc=$(grep  -oP "Accuracy:\s*\K[\d.]+"         "$amb_log"  2>/dev/null | tail -1 || echo "—")
    printf "%-40s  %-10s  %-10s\n" "$dir_name" "${bird_acc}%" "${amb_acc}%"
done
