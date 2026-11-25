# Adapter checkpoints — hashes and locations

GitHub refuses files above 100 MB, so adapters are not in this repository. Each one is
161,533,192 B (7B, r=16) or about 275 MB (14B). Local copies live under
`~/Documents/TalentHub/checkpoints/`, cluster originals under `~/runs/`.

| Arm | Checkpoint | Size | sha256 | Local copy | Cluster origin |
|---|---|---:|---|---|---|
| B (mixed corpus), best by dev-216 | step 200 | 154.1 MB | `d9dca06edeeda3d49e379004e7715914c8291e95c2bf88196b46d6bc297d83f7` | `checkpoints/runB_7b_step200/` | `~/runs/arms/cands/step200/` |
| B (mixed), other candidates | 100 / 300 / 400 / 407 / final | 154.1 MB each | recorded on the cluster at `~/runs/arms/cands/*/` | not copied | `~/runs/arms/cands/` |
| B_real (t36) | in progress | 154.1 MB | pending | pending | `~/runs/arms/cands_b_real/` |
| C, D (t30) | not run yet | — | — | — | — |
| E (t28) | not run yet | — | — | — | — |
| 14B confirmation (t26) | not run yet | ~275 MB | — | — | — |

Verify any copy with:

```bash
sha256sum checkpoints/runB_7b_step200/adapter_model.safetensors
```

Download another checkpoint (verifies the hash before writing the manifest):

```bash
.venv-tools/bin/python cluster/sync_checkpoints.py \
    --remote ~/runs/arms/cands_b_real/step200 --local checkpoints/runB_real_7b
```

The adapter is useless without its `adapter_config.json` (r, alpha, target modules) and
without the exact base revision. Both are in the same directory as the weights; the base
is `Qwen/Qwen2.5-Coder-7B-Instruct` and the run configs in `reports/` record the corpus
hash and every hyperparameter.
