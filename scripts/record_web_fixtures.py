#!/usr/bin/env python3
"""Record raw web-provider results for docs/eval/web_queries.jsonl into docs/eval/web_fixtures/*.json so ranking runs are reproducible
offline (scripts/evaluate.py --web-fixtures). Sends each query text to the configured provider chain; run only when that is acceptable.
Usage: python scripts/record_web_fixtures.py   (with .env exported)"""
import asyncio,json,sys,time
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'backend'))
import web_search as ws
async def main():
    chain=ws.build_chain();out=ROOT/'docs/eval/web_fixtures';out.mkdir(parents=True,exist_ok=True)
    for l in (ROOT/'docs/eval/web_queries.jsonl').read_text().splitlines():
        c=json.loads(l)
        for attempt in range(6):  # the scraped engines fail intermittently; recording tolerates it (the live path has a breaker instead)
            try:rows,meta=await ws.hedged_fetch(chain,{},c['query'],12,None,700,6000);break
            except ws.WebUnavailable as e:rows=None;await asyncio.sleep(4)
        if not rows:print(c['id'],'FAILED',flush=True);continue
        (out/f"{c['id']}.json").write_text(json.dumps({'id':c['id'],'query':c['query'],'recorded_at':datetime.now(timezone.utc).isoformat(),'provider':meta['provider'],'engines':ws.describe_chain(chain),'hits':[{k:r.get(k,'') for k in ('title','url','text','published')} for r in rows]},indent=1)+'\n');print(c['id'],len(rows),meta['provider'],flush=True)
        await asyncio.sleep(2)
if __name__=='__main__':asyncio.run(main())
