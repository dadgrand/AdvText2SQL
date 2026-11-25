#!/usr/bin/env python3
"""Schema-excerpt shortening experiment for the 408 long-schema rows (t31)."""
from __future__ import annotations
import collections, hashlib, json, pathlib, sys
import sqlglot
from sqlglot import exp
HERE=pathlib.Path(__file__).resolve().parent; PROJECT=HERE.parents[1]
sys.path.insert(0,str(PROJECT))
from datasets.native_pg import tokenization, renderer
MAX_LEN=tokenization.DEFAULT_MAX_LEN; RESERVE=tokenization.PROMPT_RESERVE; MIN_ANSWER=32

def load(path): return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def room(prompt_tokens): return MAX_LEN-min(prompt_tokens,MAX_LEN-RESERVE)
def pct(values,q):
    import math
    s=sorted(values); pos=(len(s)-1)*q/100; lo,hi=math.floor(pos),math.ceil(pos)
    return float(s[lo]) if lo==hi else s[lo]*(hi-pos)+s[hi]*(pos-lo)

def schema_excerpt(sql: str, db_id: str):
    schema=renderer.schema_registry().get(db_id) or {}
    tables={t['name']:{c['name']:c['type'] for c in t['columns']} for t in schema.get('tables') or []}
    if not tables: return None, None
    lookup={name.lower(): name for name in tables}
    try: tree=sqlglot.parse_one(sql, read='postgres')
    except Exception: return None, None
    alias={}; touched=[]
    for t in tree.find_all(exp.Table):
        actual=lookup.get(t.name.lower(), t.name)
        if actual in tables:
            touched.append(actual); alias[actual]=actual; alias[actual.lower()]=actual
            if t.alias:
                alias[t.alias]=actual; alias[t.alias.lower()]=actual
    touched=sorted(set(touched), key=lambda n: list(tables).index(n))
    if not touched: return None, None
    selected={t:set() for t in touched}
    unqualified=set(); star=False
    for node in tree.walk():
        if isinstance(node, exp.Star): star=True
        if isinstance(node, exp.Column):
            name=node.name; table=node.table
            if not name: continue
            if table:
                actual=alias.get(table) or alias.get(table.lower())
                if actual in selected:
                    actual_col=next((c for c in tables[actual] if c.lower()==name.lower()), name)
                    selected[actual].add(actual_col)
            else: unqualified.add(name.lower())
    for t in touched:
        for name_lower in unqualified:
            for col in tables[t]:
                if col.lower()==name_lower: selected[t].add(col)
    for t in touched:
        if star or not selected[t]: selected[t]=set(tables[t])
    lines=['Schema:']
    for t in touched:
        lines.append(f'TABLE {t}')
        for col,kind in tables[t].items():
            if col in selected[t]: lines.append(f'  - {col} ({kind})')
    return '\n'.join(lines), {'tables':touched,'columns':{t:sorted(selected[t]) for t in touched}}

def main():
    bucket=load(HERE/'train_long_schema.jsonl'); main_rows=load(HERE/'train_main.jsonl')
    tok=tokenization.load_tokenizer(); decisions=[]; short_rows=[]
    for row in bucket:
        full_ids=tokenization.encode(tok,row['prompt']); completion_ids=tokenization.encode(tok,row['completion'])
        full_example=tokenization.build_example(full_ids,completion_ids,MAX_LEN)
        excerpt,meta=schema_excerpt(row['pg_gold_sql'],row['db_id'])
        gold_expected=completion_ids+[tokenization.EOS_ID]
        original_labels=[x for x in full_example['labels'] if x!=-100]
        rec={'sample_id':row['sample_id'],'db_id':row['db_id'],
             'original_prompt_tokens':full_example['prompt_before'],'answer_tokens':full_example['answer_before'],
             'original_answer_room':full_example['answer_after'],
             'original_labels_truncated':full_example['answer_truncated']}
        if not excerpt:
            rec.update({'decision':'move_to_bucket','shortened':False,'reason':'schema excerpt unavailable'})
        else:
            pair=renderer.render(row,'train',schema_text=excerpt)
            short_ids=tokenization.encode(tok,pair['prompt'])
            short_example=tokenization.build_example(short_ids,completion_ids,MAX_LEN)
            labels=[x for x in short_example['labels'] if x!=-100]
            labels_intact = labels == gold_expected
            labels_equal_original = labels == original_labels
            room_after = MAX_LEN - short_example['prompt_after']
            usable = (labels_intact and room_after >= MIN_ANSWER
                      and short_example['answer_after'] == short_example['answer_before'])
            savings=full_example['prompt_before']-short_example['prompt_before']
            rec.update({'shortened':True,'shortened_prompt_tokens':short_example['prompt_before'],
                        'shortened_answer_room':room_after,'token_savings':savings,
                        'token_savings_share':round(savings/full_example['prompt_before'],4),
                        'gold_label_sequence_intact':labels_intact,
                        'labels_equal_original':labels_equal_original,
                        'decoded_labels':tokenization.decode_labels(tok,short_example['labels']),
                        'decision':'usable_after_shortening' if usable else 'move_to_bucket',
                        'reason':'' if usable else 'still leaves fewer than 32 answer tokens or truncates the answer',
                        'tables':meta['tables'],'columns':meta['columns']})
            short_rows.append({**row,'prompt':pair['prompt'],'schema_excerpt':excerpt,
                               'original_prompt':row['prompt'],
                               'tokens_before':full_example['prompt_before'],
                               'tokens_after':short_example['prompt_before'],
                               'decision':rec['decision']})
        decisions.append(rec)
    # artifacts
    def write_jsonl(path,rows):
        path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        return {'path':str(path),'sha256':sha(path),'rows':len(rows)}
    short_info=write_jsonl(HERE/'train_short_schema.jsonl',short_rows)
    counts=collections.Counter(d['decision'] for d in decisions)
    usable=[d for d in decisions if d['decision']=='usable_after_shortening']
    shortened=[d for d in decisions if d.get('shortened')]
    savings=[d['token_savings'] for d in shortened]
    savings_pct=[d['token_savings_share'] for d in shortened]
    report={
      'policy':f'answer_room = {MAX_LEN} - min(prompt_tokens, {MAX_LEN-RESERVE}); bucket iff answer_room < {MIN_ANSWER} '
               f'(prompt_tokens > {MAX_LEN-MIN_ANSWER})',
      'counts':{'native_total':len(main_rows)+len(bucket),'main':len(main_rows),'bucket':len(bucket),
                'dropped':0,'reconciled':len(main_rows)+len(bucket)==6907},
      'artifacts':{'train_main':{'path':str(HERE/'train_main.jsonl'),'sha256':sha(HERE/'train_main.jsonl'),'rows':len(main_rows)},
                   'train_long_schema':{'path':str(HERE/'train_long_schema.jsonl'),'sha256':sha(HERE/'train_long_schema.jsonl'),'rows':len(bucket)},
                   'train_short_schema':short_info},
      'label_sequence_note':'every bucket row had its original labels truncated by construction (answer room < 32); '
                           'the shortened prompt reproduces the complete gold label sequence (completion token ids + '
                           'the <|im_end|> EOS) exactly, so the supervised target is identical across shortenings and '
                           'no prompt token is supervised',
      'shortening':{
        'tested_rows':len(decisions),'shortened_rows':len(shortened),
        'usable_after_shortening':counts.get('usable_after_shortening',0),
        'move_to_bucket':counts.get('move_to_bucket',0),'drop':counts.get('drop',0),
        'gold_label_sequence_intact_rows':sum(1 for d in shortened if d.get('gold_label_sequence_intact')),
        'original_labels_untruncated_rows':sum(1 for d in decisions if not d.get('original_labels_truncated')),
        'labels_identical_to_original_rows':sum(1 for d in shortened if d.get('labels_equal_original')),
        'labels_identical_to_full_gold_rows':sum(1 for d in shortened if d.get('gold_label_sequence_intact')),
        'original_labels_were_all_truncated':all(d.get('original_labels_truncated') for d in decisions),
        'p50_token_savings':round(pct(savings,50),2) if savings else None,
        'p95_token_savings':round(pct(savings,95),2) if savings else None,
        'p50_savings_share':round(pct(savings_pct,50),4) if savings_pct else None,
        'p95_savings_share':round(pct(savings_pct,95),4) if savings_pct else None,
        'min_answer_room_after':min((d['shortened_answer_room'] for d in shortened),default=None),
      },
      'per_row':decisions,
      'bucket_ids':[r['sample_id'] for r in bucket],
    }
    (HERE/'short_schema_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'counts':report['counts'],'shortening':report['shortening'],
                      'train_main_sha256':report['artifacts']['train_main']['sha256'],
                      'train_long_schema_sha256':report['artifacts']['train_long_schema']['sha256']},indent=1))
if __name__=='__main__':
    raise SystemExit(
        "RETIRED (t37): this selector chooses tables/columns from pg_gold_sql and leaks a gold hint "
        "into the prompt. Use datasets/native_pg/schema_selector.py to regenerate inference-time prompts."
    )
