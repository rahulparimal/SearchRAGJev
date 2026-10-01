#!/usr/bin/env python3
"""Overview benchmark + grounding calibration (in-process pipeline against the live stack; needs .env exported).
For each answerable question: POST /search (admin, local) -> evidence -> backend overview.stream_overview with the chosen overview model and the real Jev.
Reports produced rate, withheld reasons, latency p50/p95 per model, and the grounding-score distribution of produced claims. With --calibrate it also
builds NEGATIVE claims from the same sources (altered numbers, claims from another question) and sweeps the grounding threshold.
  python scripts/overview_bench.py --models google/gemma-4-31b-it,google/diffusiongemma-26b-a4b-it --repeat 1
The user's saved settings are never changed: models are passed per run."""
import argparse,asyncio,json,os,random,re,statistics,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT/'backend'))
import urllib.request
EXTRA=['How many days of annual leave do employees get?','How long is paid parental leave for primary caregivers?','How do I connect to the corporate VPN from home?','What should I do when the VPN keeps disconnecting?','How many unused leave days can be carried over?','When does carried over annual leave expire?','How long is leave for secondary caregivers?','What client do I install to use the VPN?']
def call(api,path,tok=None,body=None):
    r=urllib.request.Request(api+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+tok} if tok else {})},method='POST' if body is not None else 'GET')
    return json.load(urllib.request.urlopen(r,timeout=60))
def pct(v,p):
    v=sorted(v);return v[min(len(v)-1,max(0,round(p/100*len(v)+0.5)-1))] if v else None
NUMW=r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twelve|sixteen|twenty|twenty-five|thirty|fifty|hundred|thousand)\b'
def alter(s):
    """A plausible-but-wrong claim from a correct one: change the first number (digits or number word); citation markers are left intact. Falls back to a negation."""
    tail=''.join(re.findall(r'\[S\d+\]',s));body=re.sub(r'\s*\[S\d+\]','',s).strip()
    m=re.search(r'\d+',body)
    if m:body=body[:m.start()]+str(int(m.group())*3+7)+body[m.end():]
    elif re.search(NUMW,body,re.I):body=re.sub(NUMW,'ninety-nine',body,count=1,flags=re.I)
    else:body=re.sub(r'\b(is|are|must|can|will)\b',lambda x:{'is':'is not','are':'are not','must':'must never','can':'cannot','will':'will not'}[x.group()],body,count=1) if re.search(r'\b(is|are|must|can|will)\b',body) else body+' This does not apply to anyone'
    return body+' '+tail
async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--api',default='http://localhost:8000');ap.add_argument('--models',default='');ap.add_argument('--repeat',type=int,default=1);ap.add_argument('--calibrate',action='store_true');ap.add_argument('--extra-body',default='');ap.add_argument('--out');ap.add_argument('--pace',type=float,default=0.0,help='seconds between questions (provider rate limits: the free NVIDIA tier answers 429 to a fast model hit at >~1 request/s)');a=ap.parse_args()
    from database import Db;from config_store import read_setting;import overview as ov;from jev import AsyncJev,ground_sentences;from providers import OverviewGenerator;from web_search import clean_text
    keys=['OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_MODEL','OVERVIEW_LLM_API_KEY','LLM_BASE_URL','LLM_MODEL','LLM_API_KEY','OVERVIEW_TIMEOUT_S','OVERVIEW_ATTEMPT_S','OVERVIEW_MAX_TOKENS','OVERVIEW_EXTRA_BODY','TYPESAFE_API_KEY','JEV_MODEL','JEV_BASE_URL']
    with Db() as db:snap={k:read_setting(db,k,k) for k in keys}
    tok=call(a.api,'/auth/login',None,{'username':os.environ['ADMIN_USERNAME'],'password':os.environ['ADMIN_PASSWORD']})['access_token']
    qs=[json.loads(l) for l in (ROOT/'docs/eval/search_benchmark.jsonl').read_text().splitlines() if l.strip()];qs=[x['query'] for x in qs if x.get('overview_expected') and x.get('type')!='acl']+EXTRA
    ev={}
    for q in qs:
        r=call(a.api,'/search',tok,{'query':q,'mode':'local','limit':5});ev[q]=[{'title':x['title'],'text':x['text'],'document_id':x['document_id'],'chunk_id':x['chunk_id'],'version':x['version'],'page':x['page'],'source':'local','rel':None} for x in r['results'][:ov.MAX_SOURCES]]
    print(f'{len(qs)} questions, tau={ov.GROUNDED}',flush=True);summary={}
    for model in [m for m in a.models.split(',') if m] or [snap['OVERVIEW_LLM_MODEL'] or snap['LLM_MODEL']]:
        s=dict(snap,OVERVIEW_LLM_MODEL=model)
        if a.extra_body:s['OVERVIEW_EXTRA_BODY']=a.extra_body
        gen=OverviewGenerator(lambda n,e:s.get(n,''));lat=[];res=[];pos=[];neg=[];rows=[]
        for rep in range(a.repeat):
            for q in qs:
                if a.pace:await asyncio.sleep(a.pace)
                jev=AsyncJev(snap['TYPESAFE_API_KEY'],snap['JEV_MODEL'],snap['JEV_BASE_URL'],900);rec={'query':q,'eligible':True,'route':'local','evidence':ev[q]};t=time.monotonic();out=[]
                async for c in ov.stream_overview(rec,jev,gen,f'bench{random.random()}'):out.append(c)
                dt=time.monotonic()-t;txt=''.join(out);got='event: overview' in txt;why=re.search(r'"reason": "(\w+)"',txt);lat.append(dt);res.append('produced' if got else (why.group(1) if why else 'none'))
                if got:
                    o=json.loads(re.search(r'event: overview\ndata: (.+)',txt).group(1));rows.append((q,o['text']))
        print(f'\n== {model}: produced {res.count("produced")}/{len(res)} ({100*res.count("produced")/len(res):.0f}%) withheld {dict((k,res.count(k)) for k in set(res) if k!="produced")} | complete p50 {pct(lat,50):.1f}s p95 {pct(lat,95):.1f}s max {max(lat):.1f}s',flush=True)
        summary[model]={'produced':res.count('produced'),'n':len(res),'p50':pct(lat,50),'p95':pct(lat,95),'max':max(lat),'withheld':{k:res.count(k) for k in set(res) if k!='produced'}}
        if a.calibrate:
            jev=AsyncJev(snap['TYPESAFE_API_KEY'],snap['JEV_MODEL'],snap['JEV_BASE_URL'],3000)
            for i,(q,text) in enumerate(rows):
                st,lines=ov.parse(text,len(ev[q]))
                if st!='ok':continue
                claims=[c for L in lines for c in L.get('sents',[])];fam=lambda k:ev[rows[k][0]][0]['title'].split('_')[0];other=next((rows[k][1].split('[S')[0] for k in range(len(rows)) if fam(k)!=fam(i)),rows[(i+1)%len(rows)][1].split('[S')[0])  # an unrelated claim from a different document family
                srcs=lambda ns:[clean_text(ev[q][n-1]['text'],ov.SOURCE_CHARS) for n in ns]
                good=await ground_sentences(jev,[(s,srcs(ns)) for s,ns in claims],query=q,lead=claims[0][0]);pos+=[g for g in good if g is not None]
                bad=await ground_sentences(jev,[(alter(s),srcs(ns)) for s,ns in claims[:2]]+[(other.strip()+' [S1].',srcs(claims[0][1]))],query=q,lead=claims[0][0]);neg+=[g for g in bad if g is not None]
            if pos and neg:
                print(f'   grounding scores: correct claims n={len(pos)} min {min(pos):.2f} p10 {pct(pos,10):.2f} p50 {pct(pos,50):.2f} | altered/unrelated claims n={len(neg)} p50 {pct(neg,50):.2f} p90 {pct(neg,90):.2f} max {max(neg):.2f}')
                for tau in (0.4,0.5,0.55,0.6,0.65,0.7,0.75,0.8,0.85):print(f'   tau {tau:.2f}: correct kept {100*sum(x>=tau for x in pos)/len(pos):3.0f}% | wrong rejected {100*sum(x<tau for x in neg)/len(neg):3.0f}%')
                summary[model]['calibration']={'pos':sorted(pos),'neg':sorted(neg)}
    if a.out:Path(a.out).write_text(json.dumps(summary,indent=1))
if __name__=='__main__':asyncio.run(main())
