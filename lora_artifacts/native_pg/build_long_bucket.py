#!/usr/bin/env python3
"""Split the native-PG corpus into the main set and the long-schema bucket (t31)."""
from __future__ import annotations
import json, pathlib, sys
HERE=pathlib.Path(__file__).resolve().parent; PROJECT=HERE.parents[1]
sys.path.insert(0,str(PROJECT))
from datasets.native_pg import tokenization
MAX_LEN=tokenization.DEFAULT_MAX_LEN; RESERVE=tokenization.PROMPT_RESERVE
MIN_ANSWER=32

def load(path): return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]
def answer_room(prompt_tokens): return MAX_LEN - min(prompt_tokens, MAX_LEN - RESERVE)

def main():
    rows=load(HERE/'native_pg_train.jsonl'); tok=tokenization.load_tokenizer()
    main_rows=[]; bucket=[]
    for row in rows:
        prompt_len=len(tokenization.encode(tok,row['prompt']))
        answer_len=len(tokenization.encode(tok,row['completion']))+1
        row['_prompt_tokens']=prompt_len; row['_answer_tokens']=answer_len
        (bucket if answer_room(prompt_len)<MIN_ANSWER else main_rows).append(row)
    def write(path, records):
        with path.open('w',encoding='utf-8') as f:
            for r in records:
                out={k:v for k,v in r.items() if not k.startswith('_')}
                f.write(json.dumps(out,ensure_ascii=False)+'\n')
    write(HERE/'train_main.jsonl',main_rows); write(HERE/'train_long_schema.jsonl',bucket)
    print(json.dumps({'total':len(rows),'main':len(main_rows),'bucket':len(bucket),
                      'policy':f'max_len={MAX_LEN}, prompt_reserve={RESERVE}, min_answer_tokens={MIN_ANSWER}, '
                               f'bucket iff answer_room < {MIN_ANSWER} (prompt_tokens > {MAX_LEN-MIN_ANSWER})',
                      'main_min_answer_room':min(answer_room(r['_prompt_tokens']) for r in main_rows),
                      'bucket_ids':[r['sample_id'] for r in bucket]},indent=1))
if __name__=='__main__': main()
