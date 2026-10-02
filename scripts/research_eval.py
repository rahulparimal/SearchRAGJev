#!/usr/bin/env python3
"""Live Deep Research eval: runs docs/eval/deep_research.jsonl against the real API and applies LLM-free structural checks
(report produced, comparison has a table with a citation on every row, enough distinct source domains, no uncited prose,
adversarial prompt not leaked, verification status). It does NOT judge factual quality: read the printed markdown against each
rubric before trusting a pass. Costs real LLM/web calls; use --limit. Bypasses the report cache (refresh=true).

A/B of the planner-skip threshold: restart the API for each arm with TOKENCACHE_ENABLED=false and DEEP_RESEARCH_ROUTE_SIMPLE_BELOW=0 (arm route0)
then 0.25 (arm route025), run with --arm/--out for each, and compare the JSONL. The web is nondeterministic, so these are proxies, not quality.

Usage (API running, Deep Research enabled): set -a; . ./.env; set +a; python scripts/research_eval.py --limit 3
"""
import argparse,json,os,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent

def login(base,u,p):
    r=urllib.request.Request(f'{base}/auth/login',data=json.dumps({'username':u,'password':p}).encode(),headers={'Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(r,timeout=30).read())['access_token']

def run(base,token,query,timeout=330):
    body=json.dumps({'query':query,'web_consent':True,'refresh':True}).encode()
    r=urllib.request.Request(f'{base}/research',data=body,headers={'Content-Type':'application/json','Authorization':f'Bearer {token}'})
    events=[];name=None
    with urllib.request.urlopen(r,timeout=timeout) as resp:
        for raw in resp:
            line=raw.decode().rstrip('\n')
            if line.startswith('event: '):name=line[7:]
            elif line.startswith('data: '):events.append((name,json.loads(line[6:])))
    return events

def check(case,events):
    d=dict(events);problems=[]
    if 'report' not in d:return [f"no report (error={d.get('error')})"],None
    rep=d['report'];blocks=rep['blocks']
    tables=[b for b in blocks if b['t']=='table']
    if case['kind']=='comparison':
        if not tables:problems.append('comparison without a table')
        for t in tables:
            if any(not any(c.get('cites') for c in row) for row in t['rows']):problems.append('table row without citation')
    domains={c['domain'] for c in rep['citations'] if c.get('domain')}
    if case['kind'] in ('comparison','survey','explainer') and len(domains)<2:problems.append(f'only {len(domains)} distinct domain(s)')
    for b in blocks:
        if b['t']=='p' and any(not r['cites'] and 'do not cover' not in r['text'] for r in b['runs']):problems.append('uncited prose run');break
    if case['kind']=='adversarial' and 'research planner' in rep['markdown'].lower():problems.append('system prompt leaked')
    if rep['verification']['status']=='unavailable':problems.append('verification unavailable (not a failure of the report, but unverified)')
    return problems,rep

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--dataset',default=str(ROOT/'docs/eval/deep_research.jsonl'));ap.add_argument('--limit',type=int,default=0)
    ap.add_argument('--base',default=f"http://localhost:{os.getenv('API_PORT','8000')}");ap.add_argument('--arm',default='',help='label for an A/B arm, e.g. route0 or route025');ap.add_argument('--out',default='',help='append one JSON line per case here');ap.add_argument('--show',action='store_true',help='print each report markdown')
    a=ap.parse_args();pw=os.getenv('ADMIN_PASSWORD')
    if not pw:sys.exit('ADMIN_PASSWORD not set (set -a; . ./.env; set +a)')
    tok=login(a.base,os.getenv('ADMIN_USERNAME','admin'),pw)
    cases=[json.loads(l) for l in open(a.dataset) if l.strip()][:a.limit or None];bad=0
    for c in cases:
        t0=time.time()
        ev=None
        try:ev=run(a.base,tok,c['query']);problems,rep=check(c,ev)
        except Exception as e:problems,rep=[f'request failed: {type(e).__name__}: {e}'],None
        hard=[p for p in problems if 'not a failure' not in p];bad+=bool(hard)
        v=rep['verification'] if rep else {};st=rep['stats'] if rep else {}
        print(f"{'PASS' if not hard else 'FAIL'} {c['id']} {c['kind']} {time.time()-t0:.0f}s sources={st.get('sources')} verif={v.get('status')} {v.get('supported')}/{v.get('checked')} {problems or ''}")
        print(f"     rubric (check manually): {c['rubric']}")
        if a.out:
            with open(a.out,'a') as f:f.write(json.dumps({'arm':a.arm,'id':c['id'],'kind':c['kind'],'ok':not hard,'seconds':round(time.time()-t0,1),'routed':(dict(ev).get('plan') or {}).get('routed') if ev else None,'llm_calls':st.get('llm_calls'),'sources':st.get('sources'),'domains':len({x['domain'] for x in rep['citations'] if x.get('domain')}) if rep else None,'verif':v.get('status'),'supported':v.get('supported'),'checked':v.get('checked'),'problems':problems})+'\n')
        if a.show and rep:print(rep['markdown'],'\n')
    print(f'\n{len(cases)-bad}/{len(cases)} structurally clean. Structure is not factual quality: review the rubric by hand.')
    sys.exit(1 if bad else 0)
main()
