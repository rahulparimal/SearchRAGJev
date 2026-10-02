#!/usr/bin/env python3
"""Relevance-floor evaluation against the live API. Runs docs/eval/search_floor_cases.jsonl (in-corpus queries with an expected document, and
out-of-corpus queries that must return nothing) with low_confidence=true and explain=true so every candidate and its floor signals come back
once; then replays the floor offline for the CURRENT thresholds and a grid of alternatives. No API restart is needed per setting.

Metrics: in-corpus RECALL (an expected passage is among the first 5 kept) and out-of-corpus FALSE-POSITIVE rate (any passage kept).
Use --local-only to test the cross-encoder path (no Jev calls); otherwise the Jev path is tested when a Jev key is configured.
Usage: set -a; . ./.env; set +a; python scripts/floor_eval.py [--local-only] [--save runs.json]"""
import argparse,itertools,json,os,sys,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
def post(base,path,tok,body):
    r=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':f'Bearer {tok}'})
    return json.loads(urllib.request.urlopen(r,timeout=90).read())
def login(base,u,p):return post(base,'/auth/login','',{'username':u,'password':p})['access_token']

def keep(c,method,th):
    ok=False;r=c.get('rel')
    if method=='jev_noul' and r is not None:ok=r>=th['jev']
    elif method=='cross_encoder' and c.get('ce_logit') is not None:ok=c['ce_logit']>=th['ce']
    if not ok:ok=c['coverage']>=th['cov'] or c.get('matched',0)>=th['matched'] or (c.get('dense') is not None and c['dense']>=th['dense'])
    return ok
def is_truth(case,c):
    if case['title_contains'].lower() not in c['title'].lower():return False
    if case.get('pages'):lo,hi=case['pages'];return lo<=c['page']<=hi
    return True
def evaluate(runs,th,floor=True):
    inn=[r for r in runs if r['case']['kind']=='in'];out=[r for r in runs if r['case']['kind']=='out']
    def kept(r):return [c for c in r['cands'] if (keep(c,r['method'],th) if floor else True)][:5]
    rec=sum(1 for r in inn if any(is_truth(r['case'],c) for c in kept(r)))/max(1,len(inn))
    fp=sum(1 for r in out if kept(r))/max(1,len(out))
    return rec,fp
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--base',default=f"http://localhost:{os.getenv('API_PORT','8000')}");ap.add_argument('--local-only',action='store_true');ap.add_argument('--save');ap.add_argument('--load')
    ap.add_argument('--dataset',default=str(ROOT/'docs/eval/search_floor_cases.jsonl'));a=ap.parse_args()
    if a.load:runs=json.load(open(a.load))
    else:
        pw=os.getenv('ADMIN_PASSWORD')
        if not pw:sys.exit('ADMIN_PASSWORD not set (set -a; . ./.env; set +a)')
        tok=login(a.base,os.getenv('ADMIN_USERNAME','admin'),pw);runs=[]
        for l in open(a.dataset):
            if not l.strip():continue
            c=json.loads(l);j=post(a.base,'/search',tok,{'query':c['query'],'mode':'local','limit':20,'low_confidence':True,'explain':True,'local_only':a.local_only})
            runs.append({'case':c,'method':j.get('reranker'),'cands':j['usage'].get('floor',[])})
        if a.save:json.dump(runs,open(a.save,'w'))
    methods=sorted({r['method'] for r in runs if r['method']});print('rerank methods seen:',methods)
    cur={'jev':float(os.getenv('REL_JEV_MIN','0.5')),'ce':float(os.getenv('REL_CE_MIN','-2.0')),'cov':float(os.getenv('REL_COV_MIN','0.75')),'matched':3,'dense':float(os.getenv('REL_DENSE_MIN','0.45'))}
    r0,f0=evaluate(runs,cur,floor=False);r1,f1=evaluate(runs,cur)
    print(f'floor OFF : recall {r0:.2f}  false-positive {f0:.2f}');print(f'floor ON  : recall {r1:.2f}  false-positive {f1:.2f}   thresholds {cur}')
    best=None
    for jev,ce,cov,mt,dn in itertools.product([0.2,0.3,0.35,0.45,0.55,0.65],[-6,-4,-2,0,2],[0.5,0.6,0.67,0.75,1.0],[2,3,99],[0.3,0.33,0.4,0.5,0.6,0.99]):
        th={'jev':jev,'ce':ce,'cov':cov,'matched':mt,'dense':dn};rc,fp=evaluate(runs,th)
        if rc>=r0-0.02 and (best is None or (fp,-rc)<(best[1],-best[0])):best=(rc,fp,th)
    if best:print(f'best within 2 points of OFF recall: recall {best[0]:.2f}  false-positive {best[1]:.2f}  thresholds {best[2]}')
    for r in runs:
        if r['case']['kind']=='out' and any(keep(c,r['method'],cur) for c in r['cands']):print('  FALSE POSITIVE:',r['case']['query'],'->',[(c['title'][:22],c['coverage'],c.get('rel'),c.get('dense')) for c in r['cands'] if keep(c,r['method'],cur)][:2])
        if r['case']['kind']=='in' and not any(is_truth(r['case'],c) for c in [c for c in r['cands'] if keep(c,r['method'],cur)][:5]):print('  MISSED:',r['case']['query'])
main()
