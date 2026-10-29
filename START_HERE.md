# START HERE — AdvText2SQL round-2 deliverable (round 3 is WIP)

**What this is.** A local, open-model text-to-SQL track for the teacher-owned AdvText2SQL harness (BIRD dev
+ Ambrosia). It measures the branch through the real harness and the live PostgreSQL, never by editing the
harness: `dev_tools/` adds the recorder, evaluator, matrix, decoder and schema-linking layers; `benchmarks/`
and `data/` stay untouched.

**The main number, with its honest framing.** The strongest in-domain result is **14B + LoRA: 130/241 =
53.94 %** (EX/209 62.20 %), against the team's frozen 42.32 % file (+11.62 pp, p = 0.000307) and against
the best 7B configuration **7B + retrieval: 117/241 = 48.55 %** (that file is a frozen predictions file,
not a live run of their pipeline). The same branch also measured the wall: on **10 unseen databases** base
7B, 7B adapter, 14B and retrieval all land at **69–73 % with no significant differences**, so the remaining
gap is **training-data coverage**, not capacity, adapter or schema presentation (round 3).

**Reading order.** `research/advtext2sql/ROUND2_RESULTS.ru.md` (one-page Russian summary) →
`docs/REPORT.md` §9–§19 (round-2 measurements and qualifiers; §20 is the round-3 negative result) →
`docs/VERIFICATION.md` (what was verified, commit/tag hashes, test counts) → `docs/RUNBOOK.md` (how to run
everything) → `docs/REPORT.md` §20 + `research/advtext2sql/ROUND3_RESULTS.ru.md` for round 3.

**Which branch and tag to use.** The delivered round-2 branch is **`feat/round2`**, teacher-facing tag
**`round2-seal-2`** (→ `cd8aa4a`; `round2-seal` → `b7dbc2d` is the earlier seal). The current branch
**`feat/schema-linking`** is **WIP round 3** and is not the deliverable; `feat/lora-track` is the frozen
round-1 branch (`f4d3d4a`).

**Reproduce in 10 minutes.** From `research/advtext2sql/repo/`: `python -m pytest tests -q` (314 passed,
1 skipped, offline); `python -m dev_tools.schema_link_dryrun --out runs/t31-schema-linking` (five modes,
no DB/GPU); then open `runs/r3-schema-link/analysis.json` and `runs/schema_linking/schema-audit.json` — the
round-3 numbers and the t24 recall audit are CPU-only replays of recorded artifacts.
