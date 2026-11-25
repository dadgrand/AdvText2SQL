#!/usr/bin/env python3
"""Reconcile the t10 batch report with the t31 long-schema policy."""
from __future__ import annotations
import collections, hashlib, json, math, pathlib, sys
HERE=pathlib.Path(__file__).resolve().parent; PROJECT=HERE.parents[1]
sys.path.insert(0,str(PROJECT))
from datasets.native_pg import tokenization
MAX_LEN=tokenization.DEFAULT_MAX_LEN; RESERVE=tokenization.PROMPT_RESERVE
REPORT=PROJECT/'cluster'/'batch_report.json'
def load(p): return [json.loads(l) for l in pathlib.Path(p).read_text(encoding='utf-8').splitlines() if l.strip()]
def sha(p): return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def pct(values,q):
    s=sorted(values); pos=(len(s)-1)*q/100; lo,hi=math.floor(pos),math.ceil(pos)
    return float(s[lo]) if lo==hi else s[lo]*(hi-pos)+s[hi]*(pos-lo)
def dist(values):
    return {'count':len(values),'min':min(values) if values else None,'max':max(values) if values else None,
            'mean':round(sum(values)/len(values),2) if values else None,
            'p50':pct(values,50) if values else None,'p95':pct(values,95) if values else None,'p99':pct(values,99) if values else None}
def measure(rows,tok):
    ctx_before=[]; ans_before=[]; ctx_after=[]; ans_after=[]; truncated=0; zero=0; prompt_trunc=0
    for r in rows:
        p=tokenization.encode(tok,r['prompt']); c=tokenization.encode(tok,r['completion'])
        ex=tokenization.build_example(p,c,MAX_LEN)
        ctx_before.append(ex['prompt_before']); ans_before.append(ex['answer_before'])
        ctx_after.append(ex['prompt_after']); ans_after.append(ex['answer_after'])
        truncated+=int(ex['answer_truncated']); zero+=int(ex['zero_loss_tokens']); prompt_trunc+=int(ex['prompt_truncated'])
    sens={}
    for limit in (512,1024,2048):
        fedp=[min(x,limit-RESERVE) for x in ctx_before]; rooms=[limit-f for f in fedp]; feda=[min(a,r) for a,r in zip(ans_before,rooms)]
        sens[str(limit)]={'truncated_prompts':sum(1 for x,f in zip(ctx_before,fedp) if x>f),
                          'truncated_answers':sum(1 for x,f in zip(ans_before,feda) if x>f),
                          'zero_loss_examples':sum(1 for f in feda if f==0),
                          'lost_answer_tokens':sum(x-f for x,f in zip(ans_before,feda)),
                          'context_p95':pct([min(x,limit) for x in ctx_before],95),
                          'answer_p95':pct(feda,95)}
    n=len(rows)
    return {'rows':n,'length_distributions':{'context_before_truncation':dist(ctx_before),'answer_before_truncation':dist(ans_before),
            'context_after_truncation':dist(ctx_after),'answer_after_truncation':dist(ans_after)},
            'truncated_answers':truncated,'truncated_answer_share':round(truncated/n,6) if n else None,
            'zero_loss_examples':zero,'prompt_truncated':prompt_trunc,'sensitivity_by_max_len':sens}
def main():
    report=json.loads(REPORT.read_text(encoding='utf-8'))
    short=json.loads((HERE/'short_schema_report.json').read_text(encoding='utf-8'))
    main_rows=load(HERE/'train_main.jsonl'); bucket=load(HERE/'train_long_schema.jsonl'); short_rows=load(HERE/'train_short_schema.jsonl')
    tok=tokenization.load_tokenizer()
    main_stats=measure(main_rows,tok)
    usable_ids={d['sample_id'] for d in short['per_row'] if d['decision']=='usable_after_shortening'}
    usable_rows=[r for r in short_rows if r['sample_id'] in usable_ids]
    combined=measure(main_rows+usable_rows,tok)
    bucket_stats=measure(bucket,tok)
    previous_policy=report.get('long_schema_policy',{})
    report['long_schema_policy']={
      'policy':short['policy'],'max_len':MAX_LEN,'prompt_reserve':RESERVE,'min_answer_tokens':32,
      'counts':short['counts'],
      'reconciliation':'6,907 native rows = 6,499 train_main + 408 train_long_schema + 0 dropped',
      'artifacts':short['artifacts'],'short_schema_report':{'path':str(HERE/'short_schema_report.json'),'sha256':sha(HERE/'short_schema_report.json')},
      'shortening':short['shortening'],
      'post_policy':{
         'train_main':main_stats,
         'train_main_plus_usable_shortened':combined,
         'bucket_remaining':bucket_stats,
         'usable_shortened_ids':sorted(usable_ids),
         'bucket_remaining_ids':sorted(set(r['sample_id'] for r in bucket)-usable_ids),
      },
      'verification':{
         'no_main_row_below_32_answer_tokens':all(x>=32 for x in [MAX_LEN-min(p,MAX_LEN-RESERVE) for p in
             [len(tokenization.encode(tok,r['prompt'])) for r in main_rows]]),
         'gold_label_sequence_intact_on_usable':short['shortening'].get('usable_label_sequence_ok',False),
         'counts_reconcile':short['counts']['native_total']==short['counts']['main']+short['counts']['bucket']+short['counts']['dropped']==6907,
      },
    }
    for key in ('selector','leakage_risk'):
        if key in previous_policy:
            report['long_schema_policy'][key]=previous_policy[key]
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'main':main_stats['rows'],'main_truncated_share':main_stats['truncated_answer_share'],
                      'combined_rows':combined['rows'],'combined_truncated_share':combined['truncated_answer_share'],
                      'bucket':bucket_stats['rows'],'usable_after_shortening':len(usable_ids),
                      'verification':report['long_schema_policy']['verification']},indent=1))
if __name__=='__main__': main()
