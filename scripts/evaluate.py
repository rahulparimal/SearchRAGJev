#!/usr/bin/env python3
"""Search benchmark harness: graded relevance (nDCG@k, MRR@k, recall@k), per-stage latency p50/p95, overview eligibility and quality,
ACL leak test, token estimates, and an offline web-fixture ranking comparison. Writes a JSON snapshot and a Markdown summary.
Examples (API running, .env exported so ADMIN_USERNAME/ADMIN_PASSWORD are available; they are used, never printed):
  python scripts/evaluate.py --label baseline_new --repeat 3
  python scripts/evaluate.py --label rerank_off --rerank off      # per-request override (admin token; nothing global is changed)
  python scripts/evaluate.py --label rerank_on --rerank on --overview
  python scripts/evaluate.py --delete-users 'evalr-*,qa-r1'       # remove leftover test users via DELETE /admin/users/{name}
  python scripts/evaluate.py --dataset docs/eval/web_queries.jsonl --mode web --web --repeat 2   # live web latency (sends queries to the web providers)
  python scripts/evaluate.py --web-fixtures docs/eval/web_fixtures                              # offline web ranking on recorded results
Corpus: ./rag seed docs/samples/eval (or python scripts/ingest_samples.py docs/samples/eval)."""
import argparse,asyncio,json,math,os,re,secrets,sys,time,urllib.error,urllib.request
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent

def metrics(results,relevant,k=20):
    """Binary metrics over document ids (legacy datasets: relevant_document_ids)."""
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

def graded(keys,rel,k=5):
    """keys: ranked result keys (duplicates dropped). rel: {key: grade 0..3}. nDCG uses gain 2^g-1; MRR counts the first grade>=2 result
    (grade>=1 when no grade-2 doc exists); recall counts grade>=1. Returns None metrics when the query has no relevant docs."""
    ranked=[];seen=set()
    for x in keys:
        if x in seen:continue
        seen.add(x);ranked.append(x)
        if len(ranked)>=k:break
    pos={g for g in rel.values() if g>0}
    if not pos:return {'ndcg':None,'mrr':None,'recall':None}
    gain=lambda g:2**g-1;dcg=sum(gain(rel.get(d,0))/math.log2(i+2) for i,d in enumerate(ranked))
    ideal=sum(gain(g)/math.log2(i+2) for i,g in enumerate(sorted(rel.values(),reverse=True)[:k]))
    thr=2 if max(rel.values())>=2 else 1
    rr=next((1/(i+1) for i,d in enumerate(ranked) if rel.get(d,0)>=thr),0.0)
    relevant=[d for d,g in rel.items() if g>=1]
    return {'ndcg':dcg/ideal if ideal else None,'mrr':rr,'recall':sum(1 for d in ranked if rel.get(d,0)>=1)/len(relevant)}

def pct(vals,p):
    v=sorted(x for x in vals if x is not None)
    return None if not v else v[min(len(v)-1,max(0,math.ceil(p/100*len(v))-1))]
def mean(vals):
    v=[x for x in vals if x is not None];return sum(v)/len(v) if v else None

def call(method,url,token=None,payload=None,timeout=90):
    req=urllib.request.Request(url,data=json.dumps(payload).encode() if payload is not None else None,method=method,headers={**({'Authorization':f'Bearer {token}'} if token else {}),'Content-Type':'application/json'})
    t=time.perf_counter()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as res:body=res.read();status=res.status
    except urllib.error.HTTPError as e:body=e.read();status=e.code
    return status,body,(time.perf_counter()-t)*1000
def post(url,token,payload):
    st,body,_=call('POST',url,token,payload)
    if st>=400:raise RuntimeError(f'HTTP {st} from {url}: {body[:200]!r}')
    return json.loads(body)
def sse(url,token,payload,timeout=60):
    """-> (events[(name,data)], ms to the last event). The whole stream is read."""
    st,body,ms=call('POST',url,token,payload,timeout)
    ev=[]
    for blk in body.decode('utf-8','replace').strip().split('\n\n'):
        m=re.match(r'event: (.+)\ndata: (.+)',blk)
        if m:ev.append((m.group(1),json.loads(m.group(2))))
    return ev,ms
def login(api,u,p):
    st,body,_=call('POST',api+'/auth/login',payload={'username':u,'password':p})
    if st!=200:raise SystemExit(f'Login failed (HTTP {st})')
    return json.loads(body)['access_token']

def run_cases(a,cases,token,forbid_titles=None):
    rows=[];api=a.api.rstrip('/');leaks=0;ov_rows=[]
    for rep in range(a.repeat):
        for c in cases:
            mode=a.mode or c.get('mode','agent');body={'query':c['query'],'limit':max(a.k,10),'mode':mode,'web':a.web or mode=='web','overview':a.overview}
            if a.rerank:body['rerank']=a.rerank=='on'   # per-request override (admin only); never flips a global setting
            if a.cold and rep==0:body['embed_cache']=False   # first pass: query embeddings are computed, not read from the cache
            t0=time.perf_counter()
            try:r=post(api+'/search',token,body)
            except RuntimeError as e:rows.append({'id':c['id'],'error':str(e)[:160]});continue
            ms=(time.perf_counter()-t0)*1000;titles=[x['title'] for x in r['results']];rel=c.get('relevant')
            m=graded(titles,rel,a.k) if rel is not None else {}
            if c.get('expect_empty'):m={'empty_ok':len(titles)==0}  # no-relevant-results cases: the correct answer is an empty list
            bad=[t for t in titles if t in set(c.get('forbidden',[]))|set(forbid_titles or [])];leaks+=len(bad)
            row={'id':c['id'],'type':c.get('type'),'rep':rep,'cold':bool(a.cold and rep==0),'ms':ms,'route':r['route'],'reranker':r['reranker'],'timings':r.get('timings',{}),'degraded':r.get('degraded_stages',[]),'top':titles[:a.k],**m,
                 'tokens':r.get('usage',{}),'overview':r.get('overview'),'leaked':bad}
            if a.overview:
                ex=c.get('overview_expected');row['eligible_match']=None if ex is None else (bool(r['overview']['eligible'])==bool(ex))
                if r.get('search_id') and r['overview']['eligible']:
                    ev,oms=sse(api+'/search/overview',token,{'search_id':r['search_id']});names=[n for n,_ in ev];got=dict(ev).get('overview');why=dict(ev).get('withheld',{}).get('reason')
                    row['overview_result']={'produced':bool(got),'withheld':why,'complete_ms':oms,'cited':len(got['citations']) if got else 0}
                    for e in (got or {}).get('citations',[]):
                        if e['title'] in set(c.get('forbidden',[]))|set(forbid_titles or []):leaks+=1;row['leaked'].append(e['title'])
            rows.append(row)
    return rows,leaks

def summarize(rows,a,leaks,cfg):
    ok=[r for r in rows if 'error' not in r];stages=['plan','bm25','fts','dense','web_fetch','web_cache','rerank','total']
    lat={s:{'p50':pct([r['timings'].get(s) for r in ok],50),'p95':pct([r['timings'].get(s) for r in ok],95),'n':len([1 for r in ok if r['timings'].get(s) is not None])} for s in stages}
    lat['client_ms']={'p50':pct([r['ms'] for r in ok],50),'p95':pct([r['ms'] for r in ok],95)}
    first=[r for r in ok if r['rep']==0];later=[r for r in ok if r['rep']>0]   # rep 0 = first time each query is seen (cold embedding cache when --cold is used); rep>0 = repeats (warm)
    split={}
    for name,rs in (('first_pass_cold' if a.cold else 'first_pass',first),('repeat_warm',later)):
        if rs:split[name]={'n':len(rs),'dense_p50':pct([r['timings'].get('dense') for r in rs],50),'dense_p95':pct([r['timings'].get('dense') for r in rs],95),'total_p50':pct([r['timings'].get('total') for r in rs],50),'total_p95':pct([r['timings'].get('total') for r in rs],95),'client_p50':pct([r['ms'] for r in rs],50),'client_p95':pct([r['ms'] for r in rs],95)}
    noresult=[r for r in ok if 'empty_ok' in r]
    scored=[r for r in ok if r.get('ndcg') is not None and r.get('type')!='acl'];by={}  # 'acl' cases test visibility, not ranking quality
    for r in scored:by.setdefault(r['type'],[]).append(r)
    q=lambda rs:{'n':len(rs),'ndcg':mean([x['ndcg'] for x in rs]),'mrr':mean([x['mrr'] for x in rs]),'recall':mean([x['recall'] for x in rs])}
    out={'label':a.label,'created_at':datetime.now(timezone.utc).isoformat(),'config':cfg,'queries':len(ok),'errors':len(rows)-len(ok),'no_results_cases':{'n':len(noresult),'correct_empty':sum(1 for r in noresult if r['empty_ok'])} if noresult else None,'k':a.k,'repeat':a.repeat,'quality':q(scored),'quality_by_type':{t:q(rs) for t,rs in by.items()},'latency_ms':lat,'cold_vs_warm':split,
         'reranker_counts':{x:sum(1 for r in ok if r['reranker']==x) for x in {r['reranker'] for r in ok}},'degraded_runs':sum(1 for r in ok if r['degraded']),'acl_leaks':leaks,
         'tokens':{'mean_packed_est':mean([r['tokens'].get('packed_tokens') for r in ok]),'mean_naive_est':mean([r['tokens'].get('naive_tokens') for r in ok]),'jev_requests_mean':mean([(r['tokens'].get('jev') or {}).get('requests') for r in ok]),'note':'word-based estimates and Jev request counts; no reduction claim is made without measuring against a stated baseline'}}
    if a.overview:
        el=[r for r in ok if r.get('eligible_match') is not None];pr=[r for r in ok if r.get('overview_result')]
        done=[r['overview_result'] for r in pr];prod=[d for d in done if d['produced']]
        out['overview']={'eligibility_match_rate':mean([1.0 if r['eligible_match'] else 0.0 for r in el]),'eligible_runs':len(pr),'produced':len(prod),'withheld_rate':(len(done)-len(prod))/len(done) if done else None,
            'withheld_reasons':{w:sum(1 for d in done if d['withheld']==w) for w in {d['withheld'] for d in done if d['withheld']}},'complete_ms':{'p50':pct([d['complete_ms'] for d in done],50),'p95':pct([d['complete_ms'] for d in done],95)},
            'time_to_first_result_ms':{'p50':pct([r['ms'] for r in ok],50),'p95':pct([r['ms'] for r in ok],95)}}
    return out

def fixtures(a):
    """Offline web ranking on recorded provider results: raw provider order vs backend rank_hits (BM25 + prior), optionally + Jev blend."""
    sys.path.insert(0,str(ROOT/'backend'));import web_search as ws
    q={json.loads(l)['id']:json.loads(l) for l in (ROOT/'docs/eval/web_queries.jsonl').read_text().splitlines() if l.strip()};res={'raw':[],'bm25_prior':[]};jev=None
    if os.getenv('TYPESAFE_API_KEY') and a.jev:
        import jev as J;jev=J.AsyncJev(os.environ['TYPESAFE_API_KEY'],os.getenv('JEV_MODEL',''),os.getenv('JEV_BASE_URL',''),900);res['bm25_prior_jev']=[]
    def label(c,h):
        d=ws.domain_of(ws.safe_url(h['url']) or 'x:');txt=(h['title']+' '+h['text']).lower()
        return 3 if any(d==x or d.endswith('.'+x) for x in c['relevant_domains']) else (2 if all(t in txt for t in c['must_terms']) else 0)
    for f in sorted(Path(a.web_fixtures).glob('*.json')):
        fx=json.loads(f.read_text());c=q.get(fx['id'])
        if not c:continue
        hits=[h for h in fx['hits'] if ws.safe_url(h['url'])];rel={}
        for h in hits:rel[h['url']]=label(c,h)
        raw=[h['url'] for h in hits];ranked=ws.rank_hits(c['query'],fx['hits'],10,table=ws.priors())
        res['raw'].append(graded(raw,rel,a.k));res['bm25_prior'].append(graded([h['url'] for h in ranked],rel,a.k))
        for h in ranked:rel.setdefault(h['url'],0)
        if jev:
            async def go():
                noul=await J.noul_scores(jev,c['query'],[{'title':h['title'],'text':h['text']} for h in ranked],10,2500)
                if not noul:return [h['url'] for h in ranked]
                order=J.blend_order([h['first'] for h in ranked],[noul[i] for i in range(len(noul))],[h['prior'] for h in ranked]);return [ranked[i]['url'] for i in order]
            res['bm25_prior_jev'].append(graded(asyncio.run(go()),rel,a.k))
    out={k:{'n':len([x for x in v if x['ndcg'] is not None]),'ndcg':mean([x['ndcg'] for x in v]),'mrr':mean([x['mrr'] for x in v]),'recall':mean([x['recall'] for x in v])} for k,v in res.items()}
    out['labels']='heuristic: grade 3 = result domain is in relevant_domains, grade 2 = title/snippet contains all must_terms (docs/eval/web_queries.jsonl); not human judgements'
    return out

def markdown(s):
    f=lambda x,n=3:'-' if x is None else (f'{x:.{n}f}' if isinstance(x,float) else str(x))
    L=[f"# Benchmark `{s['label']}` ({s['created_at'][:19]}Z)",'',f"Queries run: {s['queries']} (errors {s['errors']}), k={s['k']}, repeat={s['repeat']}, ACL leaks: **{s['acl_leaks']}**, degraded runs: {s['degraded_runs']}, rerankers: {s['reranker_counts']}",'',
       f"nDCG@{s['k']} {f(s['quality']['ndcg'])} | MRR@{s['k']} {f(s['quality']['mrr'])} | recall {f(s['quality']['recall'])} over {s['quality']['n']} scored runs",'','| type | n | nDCG | MRR | recall |','|---|---|---|---|---|']
    if s.get('no_results_cases'):L.insert(2,f"No-relevant-results cases: {s['no_results_cases']['correct_empty']}/{s['no_results_cases']['n']} correctly returned an empty list")
    L+=[f"| {t} | {q['n']} | {f(q['ndcg'])} | {f(q['mrr'])} | {f(q['recall'])} |" for t,q in sorted(s['quality_by_type'].items())]
    L+=['','| stage | p50 ms | p95 ms | n |','|---|---|---|---|']+[f"| {k} | {f(v.get('p50'),1)} | {f(v.get('p95'),1)} | {v.get('n','')} |" for k,v in s['latency_ms'].items()]
    L+=['',f"Tokens (estimates): packed {f(s['tokens']['mean_packed_est'],0)} vs naive {f(s['tokens']['mean_naive_est'],0)}; Jev requests per query {f(s['tokens']['jev_requests_mean'],1)}. {s['tokens']['note']}"]
    if s.get('cold_vs_warm'):L+=['','Cold vs warm (ms): '+json.dumps(s['cold_vs_warm'],default=lambda x:round(x,1))]
    if 'overview' in s:o=s['overview'];L+=['','Overview: '+json.dumps(o)]
    return '\n'.join(L)+'\n'

def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dataset',default=str(ROOT/'docs/eval/search_benchmark.jsonl'));ap.add_argument('--api',default=os.getenv('RAGSEARCH_API','http://localhost:8000'));ap.add_argument('--token',default=os.getenv('RAGSEARCH_TOKEN'))
    ap.add_argument('--username',default=os.getenv('ADMIN_USERNAME'));ap.add_argument('--password',default=os.getenv('ADMIN_PASSWORD'));ap.add_argument('--mode',choices=['agent','local','web']);ap.add_argument('--web',action='store_true')
    ap.add_argument('--overview',action='store_true');ap.add_argument('--k',type=int,default=5);ap.add_argument('--repeat',type=int,default=2,help='>=2 reports first pass vs repeat (warm) latency separately');ap.add_argument('--limit',type=int);ap.add_argument('--label',default='run')
    ap.add_argument('--rerank',choices=['on','off'],help='per-request Jev rerank override (administrators only; no global setting is touched). The quality pass then runs as admin and the ACL leak test runs separately as a reader with default settings')
    ap.add_argument('--cold',action='store_true',help='first pass bypasses the query-embedding cache (admin only) so cold and warm are measured separately')
    ap.add_argument('--no-reader',action='store_true');ap.add_argument('--out',default='evaluation-runs')
    ap.add_argument('--delete-users',help='comma-separated usernames or patterns (evalr-*) to delete through DELETE /admin/users/{name}, then exit')
    ap.add_argument('--web-fixtures',help='offline web ranking on recorded fixtures');ap.add_argument('--jev',action='store_true',help='with --web-fixtures: also apply the Jev rerank (needs TYPESAFE_API_KEY)');a=ap.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True);stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if a.web_fixtures:
        r=fixtures(a);p=out/f'webfixtures-{a.label}-{stamp}.json';p.write_text(json.dumps(r,indent=2));print(p);print(json.dumps(r,indent=2));return 0
    api=a.api.rstrip('/');admin=a.token or (login(api,a.username,a.password) if a.username and a.password else None)
    if not admin:ap.error('Pass --token or set ADMIN_USERNAME/ADMIN_PASSWORD (or RAGSEARCH_TOKEN)')
    if a.delete_users:
        import fnmatch
        st,body,_=call('GET',api+'/admin/users',admin)
        if st!=200:raise SystemExit(f'Cannot list users (HTTP {st}); an administrator token is required')
        names=[u['username'] for u in json.loads(body)];pats=[x.strip() for x in a.delete_users.split(',') if x.strip()]
        for n in [n for n in names if any(fnmatch.fnmatchcase(n,p) for p in pats)]:
            st,_,_=call('DELETE',api+'/admin/users/'+n,admin);print(f'delete {n}: HTTP {st}')
        return 0
    cases=[json.loads(x) for x in Path(a.dataset).read_text().splitlines() if x.strip()]
    for c in cases:  # legacy datasets use relevant_document_ids
        if 'relevant' not in c and 'relevant_document_ids' in c:c['relevant']={d:3 for d in c['relevant_document_ids']}
    if a.limit:cases=cases[:a.limit]
    override=bool(a.rerank or a.cold);reader_tok=None;name=None
    if not a.token and not a.no_reader:  # a non-admin reader sees only permitted documents: restricted ones must stay invisible
        name='evalr-'+secrets.token_hex(3);pw=secrets.token_urlsafe(24);st,body,_=call('POST',api+'/admin/users',admin,{'username':name,'password':pw,'role':'reader'})
        if st==201:reader_tok=login(api,name,pw)
        else:name=None
    token=admin if override or not reader_tok else reader_tok;leaks=0;rows=[]
    try:
        _,body,_=call('GET',api+'/admin/config',admin);cfg0=json.loads(body)
        cfg={'rerank':a.rerank or ('default ('+str(cfg0.get('jev_rerank_enabled'))+')'),'overview_model':cfg0.get('overview_llm_model') or cfg0.get('llm_model'),'web_providers':cfg0.get('web_providers'),'main_pass_as':'admin (per-request overrides)' if token==admin else 'reader','acl_test':'run as reader' if reader_tok else 'NOT RUN (no reader identity: admin/token sees restricted documents)'}
        try:post(api+'/search',token,{'query':'warm up','limit':3,'mode':'local'})  # warm-up: one throwaway query so first-request costs do not distort percentiles
        except RuntimeError:pass
        rows,l1=run_cases(a,cases,token);leaks=l1 if token!=admin else 0
        if token==admin and reader_tok:  # separate default-config reader pass for the ACL leak test only
            acl=[c for c in cases if c.get('forbidden') or c.get('type')=='acl'];b=argparse.Namespace(**dict(vars(a),rerank=None,cold=False,repeat=1,overview=False))
            _,leaks=run_cases(b,acl,reader_tok)
    finally:
        if name:  # always remove the temporary reader (an admin cannot delete itself; this is a different account)
            st,_,_=call('DELETE',api+'/admin/users/'+name,admin)
            if st!=200:print(f'WARNING: could not delete temporary reader {name} (HTTP {st}); remove it with --delete-users {name}',file=sys.stderr)
    s=summarize(rows,a,leaks if reader_tok else None,cfg);s['cases']=rows
    (out/f'bench-{a.label}-{stamp}.json').write_text(json.dumps(s,indent=2));(out/f'bench-{a.label}-{stamp}.md').write_text(markdown(s) if s['acl_leaks'] is not None else markdown(dict(s,acl_leaks='not tested')))
    print(out/f'bench-{a.label}-{stamp}.md');print(markdown(dict(s,acl_leaks=s['acl_leaks'] if s['acl_leaks'] is not None else 'not tested')));return 0
if __name__=='__main__':sys.exit(main())
