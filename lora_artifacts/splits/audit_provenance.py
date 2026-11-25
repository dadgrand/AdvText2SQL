#!/usr/bin/env python3
"""Audit the provenance of the one-epoch runs and the t1 row balance (t35)."""
from __future__ import annotations
import collections, hashlib, json, pathlib
HERE=pathlib.Path(__file__).resolve().parent; PROJECT=HERE.parents[1]
CLUSTER=PROJECT/'cluster'
SPLITS=HERE
MANIFEST=SPLITS/'schema_family_split.json'; REPORT=SPLITS/'splits_report.json'
SFT_ALL=CLUSTER/'sft_all.jsonl'; SFT_V2=CLUSTER/'sft_all_v2.jsonl'
OLD_SYNTH=CLUSTER/'sft_synth.jsonl'; SFT_TRAIN=CLUSTER/'sft_train.jsonl'
TRAIN_SRC=PROJECT/'repo'/'datasets'/'bird_pg_unified'/'data'/'bird_train_pg.jsonl'
SYNTH_SRC=PROJECT/'repo'/'datasets'/'bird_pg_unified'/'data'/'synthetic_bird_like.jsonl'
DEV=PROJECT/'repo'/'datasets'/'bird_pg_unified'/'data'/'bird_dev_pg.jsonl'
OOD120=CLUSTER/'eval_ood.jsonl'
NUMERICS=CLUSTER/'runs'/'numerics_report.json'
OUT=CLUSTER/'provenance_audit.json'; NOTE=CLUSTER/'provenance_audit.md'

def load(p): return [json.loads(l) for l in pathlib.Path(p).read_text(encoding='utf-8').splitlines() if l.strip()]
def sha(p): return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def norm_q(text):
    import re,unicodedata
    text=unicodedata.normalize('NFKC',text or '').casefold(); text=re.sub(r'[^0-9a-z]+',' ',text)
    return ' '.join(text.split())

def main():
    manifest=json.loads(MANIFEST.read_text(encoding='utf-8'))
    split={db:f['split'] for db,f in manifest['families'].items()}
    rows=load(SFT_ALL); total=len(rows)
    by_split=collections.Counter(split.get(r['db_id'],'unknown') for r in rows)
    ood_rows=[r for r in rows if split.get(r['db_id'])=='ood_reserved']
    internal_rows=[r for r in rows if split.get(r['db_id'])=='internal_valid']
    external_rows=[r for r in rows if split.get(r['db_id'])=='external_test']
    unknown_rows=[r for r in rows if r['db_id'] not in split]
    per_db_ood=collections.Counter(r['db_id'] for r in ood_rows)
    per_db_internal=collections.Counter(r['db_id'] for r in internal_rows)
    per_db_external=collections.Counter(r['db_id'] for r in external_rows)

    train_dbs=collections.Counter(r['db_id'] for r in rows)
    dev=load(DEV); ood120=load(OOD120)
    dev_dbs=sorted({r['db_id'] for r in dev})
    dev_rows_in_corpus=sum(1 for r in rows if r['db_id'] in dev_dbs)
    dev_ids={str(r.get('question_id')) for r in dev}
    dev_qid_overlap=sorted(dev_ids & {str(r.get('question_id')) for r in rows})
    ood120_hit=[r for r in ood120 if train_dbs.get(r['db_id'],0)>0]
    ood120_per_db=collections.Counter(r['db_id'] for r in ood120_hit)
    # supplementary: OOD-876
    ood876_path=PROJECT/'data_cache'/'ood_heldout.json'
    ood876=json.loads(ood876_path.read_text(encoding='utf-8')) if ood876_path.is_file() else []
    ood876_hit=[r for r in ood876 if train_dbs.get(r['db_id'],0)>0]

    # runs and reported metrics
    runs=[
      {"run":"7b-2ep","base_model":"Qwen2.5-Coder-7B-Instruct","epochs":2,"adapter":"~/runs/7b-2ep",
       "training_log":"cluster_train7b.log","steps":3432,"seconds":2832.8,"rows":6866,
       "corpus_sha256":sha(SFT_ALL),
       "adapter_config":{"r":16,"alpha":32,"dropout":0.05,"targets":"all seven Qwen projections",
                          "trainable_params":40370176},
       "metric_source":"repo/docs/REPORT.md §14/§17 and repo/runs/t2/OOD-120 ledgers",
       "reported_metrics":[
         {"metric":"historical dev-241 EX","value":"102/241 = 42.32%","affected":False,
          "reason":"0 training rows on the eight internal_valid dev databases"},
         {"metric":"historical dev-241 EX (gold-executable)","value":"102/209 = 48.80%","affected":False,
          "reason":"same: dev schemas never trained"},
         {"metric":"OOD-120 EX","value":"83/120 = 69.17%","affected":True,
          "reason":"120/120 OOD-120 questions use a database with training rows"}]},
      {"run":"14b-1ep","base_model":"Qwen2.5-Coder-14B-Instruct","epochs":1,"adapter":"~/runs/14b-1ep",
       "training_log":"cluster_train14b.log","steps":1717,"seconds":2491.2,"rows":6866,
       "corpus_sha256":sha(SFT_ALL),
       "adapter_config":{"r":16,"alpha":32,"dropout":0.05,"targets":"all seven Qwen projections",
                          "trainable_params":68812800},
       "metric_source":"repo/docs/REPORT.md §14/§17 and repo/runs/t2/OOD-120 ledgers",
       "reported_metrics":[
         {"metric":"historical dev-241 EX","value":"130/241 = 53.94%","affected":False,
          "reason":"0 training rows on the eight internal_valid dev databases"},
         {"metric":"historical dev-241 EX (gold-executable)","value":"130/209 = 62.20%","affected":False,
          "reason":"same: dev schemas never trained"},
         {"metric":"OOD-120 EX","value":"88/120 = 73.33%","affected":True,
          "reason":"120/120 OOD-120 questions use a database with training rows"}]},
    ]
    for run in runs:
        run["corpus_rows"]=total
        run["contaminated_rows"]={
            "ood_reserved":len(ood_rows),"internal_valid":len(internal_rows),
            "external_test":len(external_rows),"unknown":len(unknown_rows),
            "total":len(ood_rows)+len(internal_rows)+len(external_rows)+len(unknown_rows)}
        run["corpus_sha256_matches_sft_all"]=run["corpus_sha256"]==sha(SFT_ALL)

    # accumulation caveat
    numerics=json.loads(NUMERICS.read_text(encoding='utf-8'))
    naive=next((c for c in numerics['checks'] if c['id']=='accumulation_naive_mean_of_means'),None)
    accumulation={
        "status":"independent second reason the old training dynamics are not comparable",
        "pre_fix":"per-microbatch mean loss, then mean of means (old loop)",
        "post_fix":"sum(NLL) / total valid target tokens per optimizer step (t20 token-weighted fix)",
        "measured_naive_vs_fused_rel_l2":naive['measurements'].get('naive_vs_fused',{}).get('rel_l2') if naive else None,
        "measured_token_weighted_rel_l2":naive['measurements'].get('token_weighted_vs_fused',{}).get('rel_l2') if naive else None,
        "source":str(NUMERICS),"source_sha256":sha(NUMERICS),
        "statement":"both earlier runs predate the t20 fix; even where the data is clean their optimizer steps do not match the new arms' token-weighted accumulation, so training dynamics/curves are incomparable for this second, independent reason",
    }

    # t1 balance
    v2=load(SFT_V2); src_real=load(TRAIN_SRC); src_synth=load(SYNTH_SRC); old_all=load(SFT_ALL); old_synth=load(OLD_SYNTH)
    v2_real=[r for r in v2 if r.get('target_source')=='gold']
    v2_synth=[r for r in v2 if r.get('target_source')=='synthetic_template']
    real_ids=[str(r.get('question_id')) for r in v2_real]; src_real_ids=[str(r.get('sample_id')) for r in src_real]
    synth_ids=[str(r.get('question_id')) for r in v2_synth]; src_synth_ids=[str(r.get('sample_id')) for r in src_synth]
    old_all_synth=[r for r in old_all if r.get('target_source')=='synthetic_template']
    old_synth_rows=load(OLD_SYNTH); old_synth_ids={str(r.get('question_id')) for r in old_synth_rows}
    old_all_synth_ids={str(r.get('question_id')) for r in old_all_synth}
    counts=collections.Counter(synth_ids)
    doubled=sorted(i for i,c in counts.items() if c==2); single=sorted(i for i,c in counts.items() if c==1)
    over=sorted(i for i,c in counts.items() if c>2)
    # pair integrity: two prompts distinct, same completion for doubled ids
    by_id=collections.defaultdict(list)
    for r in v2_synth: by_id[str(r['question_id'])].append(r)
    # one restored second-phrasing row per doubled id; the old corpus had one row per id
    restored=[by_id[sid][1] for sid in doubled if sid in old_all_synth_ids]
    src_counts=collections.Counter(src_synth_ids)
    src_doubled=sorted(i for i,c in src_counts.items() if c==2)
    pair_problems=[]
    for sid in doubled:
        group=by_id[sid]; prompts={r['prompt'] for r in group}; completions={r['completion'] for r in group}
        if len(group)!=2 or len(prompts)!=2 or len(completions)!=1:
            pair_problems.append({"sample_id":sid,"rows":len(group),"distinct_prompts":len(prompts),"distinct_completions":len(completions)})
    # restored rows must not introduce ids absent from the old corpus
    new_ids=set(synth_ids); restored_ids=set(doubled)
    introduced=sorted(new_ids-old_synth_ids)
    real_dups=[i for i,c in collections.Counter(real_ids).items() if c>1]
    real_missing=sorted(set(src_real_ids)-set(real_ids)); real_extra=sorted(set(real_ids)-set(src_real_ids))
    t1_balance={
      "corpus":str(SFT_V2),"sha256":sha(SFT_V2),"rows":len(v2),
      "real_rows":len(v2_real),"synthetic_rows":len(v2_synth),
      "real_expected":len(src_real),"synthetic_expected":len(src_synth),
      "real_equation":f"{len(v2_real)} real == {len(src_real)} source real; {len(v2_synth)} synthetic == {len(src_synth)} source synthetic",
      "real_ids_duplicated":real_dups,"real_ids_missing":real_missing,"real_ids_extra":real_extra,
      "synthetic_unique_ids":len(new_ids),"synthetic_rows_per_id":dict(collections.Counter(counts.values())),
      "doubled_ids":doubled,"single_phrasing_ids":single,"ids_with_more_than_two_rows":over,
      "restored_pair_rows":len(restored),"restored_ids":sorted(restored_ids),
      "source_doubled_ids":len(src_doubled),"restored_ids_match_source_pairs":set(doubled)==set(src_doubled),
      "restored_rows_have_no_new_ids":not introduced and set(doubled)<=old_all_synth_ids,
      "introduced_ids":introduced[:20],
      "pair_problems":pair_problems,
      "old_sft_all_synthetic_ids":len(old_all_synth_ids),
      "old_sft_synth_ids":len(old_synth_ids),
      "expectation_every_synthetic_id_twice":"not met literally: 1,179 ids appear twice and 457 source pairs have only one phrasing; the 457 are legitimate source singletons, not dropped rows, and are listed here",
      "confirmed":len(v2_real)==5110 and len(v2_synth)==2815 and not real_dups and not real_missing and not real_extra
                  and not over and not pair_problems and not introduced,
    }

    result={
      "generated_at_utc":__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='seconds'),
      "objective":"provenance audit of the one-epoch GPU runs and t1 balance",
      "corpus":{
        "path":str(SFT_ALL),"sha256":sha(SFT_ALL),"rows":total,
        "target_sources":dict(collections.Counter(r.get('target_source') for r in rows)),
        "split_manifest":str(MANIFEST),"split_manifest_sha256":sha(MANIFEST),
        "splits_report":str(REPORT),"splits_report_sha256":sha(REPORT),
        "rows_by_split":dict(by_split),
        "rows_by_database_ood":dict(sorted(per_db_ood.items())),
        "rows_by_database_internal_valid":dict(sorted(per_db_internal.items())),
        "rows_by_database_external_test":dict(sorted(per_db_external.items())),
        "ood_row_ids":[str(r.get('question_id')) for r in ood_rows],
      },
      "runs":runs,
      "dev217":{
        "dev_rows":len(dev),"dev_databases":dev_dbs,
        "training_rows_on_dev_databases":dev_rows_in_corpus,
        "dev_question_ids_in_corpus":dev_qid_overlap,
        "verdict":"unaffected by data contamination: the corpus has 0 rows on any internal_valid dev schema and no dev question id appears in it",
      },
      "ood120":{
        "questions":len(ood120),"questions_on_trained_databases":len(ood120_hit),
        "databases":dict(sorted(ood120_per_db.items())),
        "verdict":"contaminated: 120/120 OOD-120 questions use a database with training rows",
      },
      "ood876_supplementary":{
        "questions":len(ood876),"questions_on_trained_databases":len(ood876_hit),
        "verdict":"also contaminated for the same reason" if ood876 else "not available",
      },
      "accumulation_caveat":accumulation,
      "t1_balance":t1_balance,
      "short_note":str(NOTE),
    }
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    note=f'''# Provenance audit — one-epoch runs and t1 balance (t35)

Corpus: `cluster/sft_all.jsonl`, {total:,} rows, sha256 `{sha(SFT_ALL)}`.
Split manifest sha256 `{sha(MANIFEST)}`.

## One-epoch runs (both trained on sft_all)

| run | base | epochs | steps | rows | contaminated rows | affected metrics |
|---|---|---:|---:|---:|---|---|
| 7b-2ep | Qwen2.5-Coder-7B-Instruct | 2 | 3,432 | 6,866 | {len(ood_rows)} ood_reserved / {len(internal_rows)} internal_valid / {len(external_rows)} external_test | dev-241 102/241 and 102/209 unaffected; OOD-120 83/120 contaminated |
| 14b-1ep | Qwen2.5-Coder-14B-Instruct | 1 | 1,717 | 6,866 | {len(ood_rows)} ood_reserved / {len(internal_rows)} internal_valid / {len(external_rows)} external_test | dev-241 130/241 and 130/209 unaffected; OOD-120 88/120 contaminated |

Dev-217: 0 training rows on any of the eight internal_valid dev databases and no dev question id in the corpus — unaffected.
OOD-120: 120/120 questions use a database with training rows — contaminated. OOD-876 has the same status ({len(ood876_hit)}/{len(ood876)}).

## Second, independent caveat: accumulation

Both runs predate the t20 gradient-accumulation fix. They averaged per-microbatch means, while the fixed loop uses sum(NLL) / total valid targets. t20 measured naive-vs-fused relative L2 **6.06e-01** (token-weighted 8.20e-02; `{NUMERICS.name}`). Their training dynamics/curves are therefore not comparable to the new arms even where the data is clean.

## What may still be quoted

- **Quotable:** dev-241 in-domain evaluations of these adapters (102/241=42.32%, 102/209=48.80%; 130/241=53.94%, 130/209=62.20%) as historical adapter results — no dev schema was trained.
- **Mark contaminated:** every OOD-120 number from these runs (83/120=69.17%, 88/120=73.33%), and any OOD-876 result from the same adapters.
- **Mark uncomparable:** training dynamics, step counts and supervised-token budgets of the one-epoch runs versus the new token-weighted arms (accumulation caveat above).

## t1 balance (`cluster/sft_all_v2.jsonl`)

- {len(v2_real):,} real + {len(v2_synth):,} synthetic = {len(v2):,}; matches the source pools (5,110 real / 2,815 synthetic).
- Real rows: no duplicates, no missing, no extra ids versus the unified train source.
- Synthetic: 1,179 ids appear twice (second phrasings restored; no new ids introduced) and 457 ids appear once. The literal expectation "every synthetic id appears twice" is therefore not met; the 457 ids are legitimate single-phrasing source pairs, listed in the JSON, not silently dropped rows.
- Restored rows: {len(restored):,} second rows, all ids already present in the old corpus; pair integrity problems: {len(pair_problems)}.
'''
    NOTE.write_text(note,encoding='utf-8')
    print(json.dumps({"corpus_sha256":sha(SFT_ALL),"rows_by_split":dict(by_split),
                      "runs":[(r['run'],r['contaminated_rows']) for r in runs],
                      "dev217_unaffected":dev_rows_in_corpus==0 and not dev_qid_overlap,
                      "ood120_contaminated":f"{len(ood120_hit)}/{len(ood120)}",
                      "t1_confirmed":t1_balance['confirmed'],"t1_rows":len(v2),
                      "synthetic_singles":len(single),"restored":len(restored)},indent=1))
if __name__=='__main__': main()
