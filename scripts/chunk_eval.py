#!/usr/bin/env python3
"""Live page-relevance eval for a single large/messy document (e.g. a scanned PDF), where the normal document-id graded-relevance
harness (scripts/evaluate.py) is meaningless (one document, many pages). Hits the real running API's /search in local mode and grades
each query by whether any returned chunk's page falls inside a ground-truth page range, or (negative_control) returns no hits at all.

Ground truth format (docs/eval/siva_purana.jsonl), one JSON object per line:
  {"id": "...", "query": "...", "pages": [start, end], "must_contain_any": ["word", ...]}   # page-hit + optional keyword check
  {"id": "...", "query": "...", "negative_control": true}                                    # must return zero hits from the target doc

Usage (API running, admin credentials in .env):
  python scripts/chunk_eval.py --dataset docs/eval/siva_purana.jsonl --title "Siva Purana Part 1" --label siva_purana_v1
"""
import argparse,json,os,sys,time,urllib.error,urllib.request
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent

def req(method,url,token=None,body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(url,data=data,method=method,headers={'Content-Type':'application/json',**({'Authorization':f'Bearer {token}'} if token else {})})
    with urllib.request.urlopen(r,timeout=30) as resp:return json.loads(resp.read())

def login(base,user,pw):return req('POST',f'{base}/auth/login',body={'username':user,'password':pw})['access_token']

def grade(case,results,title):
    hits=[r for r in results if r.get('title')==title]
    if case.get('negative_control'):
        return {'ok':not hits,'detail':f'{len(hits)} hits from target doc (want 0)'}
    lo,hi=case['pages']
    page_hit=any(lo<=r.get('page',-1)<=hi for r in hits)
    kw=case.get('must_contain_any') or []
    kw_hit=(not kw) or any(any(k.lower() in r.get('text','').lower() for k in kw) for r in hits)
    best_rank=next((r['rank'] for r in hits if lo<=r.get('page',-1)<=hi),None)
    return {'ok':page_hit and kw_hit,'detail':f'page_hit={page_hit} kw_hit={kw_hit} best_rank={best_rank} hits_pages={[r.get("page") for r in hits][:5]}'}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--dataset',required=True)
    ap.add_argument('--title',required=True,help='exact document title as shown in /admin/documents, used to isolate hits')
    ap.add_argument('--base',default=f"http://localhost:{os.getenv('API_PORT','8000')}")
    ap.add_argument('--label',default='chunk_eval')
    ap.add_argument('--limit',type=int,default=8)
    ap.add_argument('--local-only',action='store_true',help='forces local_only:true (exercises the local cross-encoder fallback reranker instead of Jev)')
    args=ap.parse_args()
    user=os.getenv('ADMIN_USERNAME','admin');pw=os.getenv('ADMIN_PASSWORD')
    if not pw:sys.exit('ADMIN_PASSWORD not set (export .env first: set -a; . ./.env; set +a)')
    token=login(args.base,user,pw)
    cases=[json.loads(l) for l in Path(args.dataset).read_text().splitlines() if l.strip()]
    rows=[]
    for c in cases:
        t0=time.perf_counter()
        try:out=req('POST',f'{args.base}/search',token,{'query':c['query'],'mode':'local','local_only':args.local_only,'limit':args.limit})
        except urllib.error.HTTPError as e:rows.append({'id':c['id'],'query':c['query'],'ok':False,'detail':f'HTTP {e.code}','ms':None});continue
        ms=round((time.perf_counter()-t0)*1000)
        g=grade(c,out.get('results',[]),args.title)
        rows.append({'id':c['id'],'query':c['query'],'ms':ms,**g})
    passed=sum(1 for r in rows if r['ok']);total=len(rows)
    ts=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_dir=ROOT/'evaluation-runs';out_dir.mkdir(exist_ok=True)
    json_path=out_dir/f'chunkeval-{args.label}-{ts}.json'
    json_path.write_text(json.dumps({'dataset':args.dataset,'title':args.title,'passed':passed,'total':total,'rows':rows},indent=2))
    md=[f'# Chunk eval: {args.label}',f'Dataset: `{args.dataset}` against document `{args.title}`',f'**{passed}/{total} passed**','','| id | query | ok | latency ms | detail |','|---|---|---|---|---|']
    for r in rows:md.append(f"| {r['id']} | {r['query'][:60]} | {'PASS' if r['ok'] else 'FAIL'} | {r.get('ms')} | {r.get('detail','')} |")
    (out_dir/f'chunkeval-{args.label}-{ts}.md').write_text('\n'.join(md))
    print('\n'.join(md))
    print(f'\nWrote {json_path}')

if __name__=='__main__':main()
