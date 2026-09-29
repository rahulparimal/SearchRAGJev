#!/usr/bin/env python3
"""Run a labeled JSONL search benchmark and save a versioned evaluation snapshot."""
import argparse,json,math,os,time,urllib.request
from datetime import datetime,timezone
from pathlib import Path

def metrics(results,relevant,k=20):
    relevant=set(relevant);k=max(0,int(k));ranked=[];seen=set()
    for row in results:
        if len(ranked)>=k:break
        doc_id=row.get('document_id')
        if doc_id is None or doc_id in seen:continue
        seen.add(doc_id);ranked.append(doc_id)
        if len(ranked)>=k:break
    found=sum(1 for d in ranked if d in relevant)
    rr=next((1/(i+1) for i,d in enumerate(ranked) if d in relevant),0.0)
    dcg=sum((1/math.log2(i+2)) for i,d in enumerate(ranked) if d in relevant)
    ideal=sum(1/math.log2(i+2) for i in range(min(len(relevant),k)))
    return {'recall_at_k':found/len(relevant) if relevant else None,'mrr':rr,'ndcg_at_k':dcg/ideal if ideal else None,'k':k}

def post(url,token,payload):
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=90) as res:return json.loads(res.read())

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--dataset',required=True,help='UTF-8 JSONL with id, query, relevant_document_ids');ap.add_argument('--api',default='http://localhost:8000');ap.add_argument('--token',default=os.getenv('RAGSEARCH_TOKEN'));ap.add_argument('--out',default='evaluation-runs');args=ap.parse_args()
    if not args.token:ap.error('Pass --token or set RAGSEARCH_TOKEN')
    cases=[json.loads(x) for x in Path(args.dataset).read_text().splitlines() if x.strip()]
    rows=[]
    for case in cases:
        response=post(args.api.rstrip('/')+'/search',args.token,{'query':case['query'],'limit':20,'answer':False})
        m=metrics(response['results'],case.get('relevant_document_ids',[]),20)
        rows.append({'id':case.get('id'),**m,'token_usage':response.get('usage',{}),'retrieved':[{'document_id':r['document_id'],'page':r['page'],'chunk_id':r['chunk_id'],'rank':r['rank']} for r in response.get('results',[])],'intent':response.get('intent'),'reranker':response.get('reranker')})
    aggregate={}
    for key in ('recall_at_k','mrr','ndcg_at_k'):
        vals=[r[key] for r in rows if r[key] is not None];aggregate[key]=sum(vals)/len(vals) if vals else None
    reductions=[r['token_usage'].get('reduction_pct',0) for r in rows]
    aggregate['mean_estimated_token_reduction_pct']=sum(reductions)/len(reductions) if reductions else 0
    snapshot={'created_at':datetime.now(timezone.utc).isoformat(),'api':args.api,'dataset':str(Path(args.dataset).resolve()),'case_count':len(rows),'aggregate':aggregate,'cases':rows,'limitations':'Token counts and savings are word-based estimates. Compare actual provider usage and answer citation quality before making a savings claim.'}
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True);dest=out/f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json";dest.write_text(json.dumps(snapshot,indent=2));print(dest);print(json.dumps(aggregate,indent=2))
if __name__=='__main__':main()
