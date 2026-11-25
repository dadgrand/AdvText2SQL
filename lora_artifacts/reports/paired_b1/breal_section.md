## B_real comparisons (batch 1)

Per-id ledgers: `paired_ood876_breal.jsonl` (B_real vs A), `paired_ood876_breal_vs_bmixed.jsonl` (B_real vs B_mixed), `paired_dev216_breal.jsonl`, `paired_dev216_breal_vs_bmixed.jsonl`; per-database aggregates in `by_db_breal.json`.

| comparison | shard | EX baseline → current | Δ | 95 % CI | fixes / regressions | net | McNemar p | ledger |
|---|---|---|---|---|---:|---:|---:|---|
| A → B_mixed | ood876 | 574/811 → 638/811 | +7.89 п.п. | [+5.06; +10.85] | 105 / 41 | +64 | 1.17e-07 | `paired_ood876.jsonl` |
| A → B_real | ood876 | 574/811 → 649/811 | +9.25 п.п. | [+6.41; +12.21] | 112 / 37 | +75 | 5.79e-10 | `paired_ood876_breal.jsonl` |
| B_mixed → B_real | ood876 | 638/811 → 649/811 | +1.36 п.п. | [-0.74; +3.33] | 40 / 29 | +11 | 0.228 | `paired_ood876_breal_vs_bmixed.jsonl` |
| A → B_real | dev216 | 87/216 → 103/216 | +7.41 п.п. | [+0.93; +13.89] | 35 / 19 | +16 | 0.0402 | `paired_dev216_breal.jsonl` |
| B_mixed → B_real | dev216 | 101/216 → 103/216 | +0.93 п.п. | [-4.63; +6.48] | 20 / 18 | +2 | 0.871 | `paired_dev216_breal_vs_bmixed.jsonl` |

**The +1.36 pp of B_real over B_mixed (OOD-876)** is the balance of 40 fixes and 29 regressions over 69 discordant questions. The 69 discordant questions (net +11) are **concentrated**: `movies_4` alone carries +6 of the net +11 (55 %), and `law_episode` -2, `music_tracker` -3 offset it on the other side — so the +1.36 pp is **not a general effect**.

Per database (net of fixes − regressions): `movie_3` +3; `menu` +0; `movies_4` +6; `talkingdata` +2; `disney` +1; `law_episode` -2; `european_football_1` +3; `sales_in_weather` +1; `music_tracker` -3; `social_media` +0.

Reading rules: dev-216 is the selection shard (all dev numbers are optimistic by construction); OOD-876 is confirmation only; every number is batch-1 decoding; the historical 241 shard was not run for B_real or B_mixed, so no 241 cell exists here (not a zero).

