#!/usr/bin/env python3
"""Offline tuning of the local Jev rerank blend with a dev/holdout split (even-numbered scored queries = dev, odd = holdout).
Candidates and Jev scores are collected once through the real backend functions (live Postgres/Qdrant/Jev; .env exported) and cached as JSON, then
blend weights and top-N are swept offline. Weights are chosen on dev ONLY; holdout is reported untouched so overfitting is visible.
  python scripts/tune_rerank.py --cache evaluation-runs/rerank_scores.json"""
import argparse,asyncio,itertools,json,os,statistics,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT/'backend'));sys.path.insert(0,str(ROOT/'scripts'))
from evaluate import graded
def m(v):v=[x for x in v if x is not None];return statistics.mean(v) if v else 0.0
async def collect(cases,cache,top_n,batch=None,chars=None):
    import app as A,jev as J
    from providers import QdrantVectors
    A.vectors=QdrantVectors();p=A.P('tune-reader','reader');j=J.AsyncJev(os.environ['TYPESAFE_API_KEY'],os.getenv('JEV_MODEL',''),os.getenv('JEV_BASE_URL',''),4000)
    from retrieval import reciprocal_rank_fusion,parse_weights
    for c in cases:
        if c['id'] in cache:continue
        q=c['query'];permitted=A.permitted_for(p);bm=A.leg_bm25(q,permitted);fts=A.leg_fts(q,p);dense=A.leg_dense(q,permitted)
        if not (bm or fts) and dense and max(h.score for h in dense)<A.DENSE_ALONE:dense=[]
        fused,_=reciprocal_rank_fusion(dense,fts,bm,weights=parse_weights(os.getenv('RRF_WEIGHTS')));sel=A.load_local(fused,p,top_n)
        noul=await J.noul_scores_batch(j,q,[{'title':d.title,'text':ch.text} for _,ch,d in sel],None,4000,batch=batch,chars=chars)
        cache[c['id']]={'titles':[d.title for _,_,d in sel],'first':[s for s,_,_ in sel],'noul':{str(i):v for i,v in (noul or {}).items()}}
    return j.stats
def ndcg_mrr(cases,cache,w,n):
    from jev import blend_order
    out=[]
    for c in cases:
        e=cache[c['id']];k=min(n,len(e['titles']))
        if k<2:order=list(range(len(e['titles'])))
        else:order=blend_order(e['first'],[e['noul'].get(str(i)) for i in range(k)],None,w)
        r=graded([e['titles'][i] for i in order],c['relevant'],5);out.append((r['ndcg'],r['mrr']))
    return m([x[0] for x in out]),m([x[1] for x in out])
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--cache',default=str(ROOT/'evaluation-runs/rerank_scores.json'));ap.add_argument('--top-n',type=int,default=30);ap.add_argument('--batch',type=int);ap.add_argument('--chars',type=int,help='state characters per Jev request (default JEV_BATCH_CHARS)');a=ap.parse_args()
    cases=[json.loads(l) for l in (ROOT/'docs/eval/search_benchmark.jsonl').read_text().splitlines() if l.strip()];cases=[c for c in cases if c.get('type')!='acl' and c.get('relevant')]
    cp=Path(a.cache);cache=json.loads(cp.read_text()) if cp.exists() else {}
    if any(c['id'] not in cache for c in cases):print('collecting',asyncio.run(collect(cases,cache,a.top_n,a.batch,a.chars)));cp.parent.mkdir(parents=True,exist_ok=True);cp.write_text(json.dumps(cache))
    dev,hold=cases[0::2],cases[1::2];print(f'{len(cases)} scored queries: dev {len(dev)}, holdout {len(hold)}')
    base=lambda cs:ndcg_mrr(cs,cache,(0,1,0),2)  # w_noul=0: first-stage order only
    print('first-stage only (no rerank): dev nDCG %.3f MRR %.3f | holdout nDCG %.3f MRR %.3f | all nDCG %.3f'%(*base(dev),*base(hold),base(cases)[0]))
    grid=[(a_,b_,0.0) for a_ in (0.3,0.5,0.65,0.8,1.0) for b_ in (0.0,0.1,0.25,0.5)]
    for n in (10,16,24,30):
        res=sorted(grid,key=lambda w:-ndcg_mrr(dev,cache,w,n)[0])[:2];import jev as J;dflt=(J.DEFAULT_WEIGHTS[0],J.DEFAULT_WEIGHTS[1],0.0)
        for w in [dflt]+res:print(f'top_n={n:2d} weights(noul,first,prior)={w}: dev nDCG %.3f MRR %.3f | holdout nDCG %.3f MRR %.3f | all nDCG %.3f'%(*ndcg_mrr(dev,cache,w,n),*ndcg_mrr(hold,cache,w,n),ndcg_mrr(cases,cache,w,n)[0]))
    import jev as J;dw=(J.DEFAULT_WEIGHTS[0],J.DEFAULT_WEIGHTS[1],0.0)
    for name,w in (('old (0.65,0.25)',(0.65,0.25,0.0)),('current default',dw)):
        worse=sum(1 for c in cases if ndcg_mrr([c],cache,w,24)[0]<ndcg_mrr([c],cache,(0,1,0),2)[0]-1e-9);better=sum(1 for c in cases if ndcg_mrr([c],cache,w,24)[0]>ndcg_mrr([c],cache,(0,1,0),2)[0]+1e-9);print(f'{name}, top_n=24: better than first-stage on {better}, worse on {worse}, of {len(cases)} queries')
if __name__=='__main__':main()
