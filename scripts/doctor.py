#!/usr/bin/env python3
"""`./rag doctor`: one line per check with a fix hint. Stdlib only. Exit 0 when nothing FAILed, else 1. Secrets are used but never printed.
WARN = degraded or optional; SKIP = not applicable."""
import argparse,json,os,platform,re,shutil,socket,subprocess,sys,time,urllib.error,urllib.request
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(Path(__file__).resolve().parent))
from setup_env import parse
R=[]
def rec(level,name,msg,hint=''):
    R.append(level);print(f"[{level:4}] {name}: {msg}"+(f"\n         fix: {hint}" if hint and level in ('FAIL','WARN') else ''))
def run(*cmd,timeout=15):
    try:p=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout,cwd=ROOT);return p.returncode,p.stdout.strip(),p.stderr.strip()
    except (OSError,subprocess.TimeoutExpired) as e:return 127,'',str(e)
def http(method,url,headers=None,body=None,timeout=6):
    """-> (status|None, parsed-json-or-text, seconds). Never raises."""
    t=time.time();data=json.dumps(body).encode() if body is not None else None;h=dict(headers or {})
    if data:h['Content-Type']='application/json'
    try:
        with urllib.request.urlopen(urllib.request.Request(url,data=data,headers=h,method=method),timeout=timeout) as r:raw=r.read().decode('utf-8','replace');st=r.status
    except urllib.error.HTTPError as e:raw=e.read().decode('utf-8','replace');st=e.code
    except Exception as e:return None,str(e)[:80],time.time()-t
    try:return st,json.loads(raw),time.time()-t
    except ValueError:return st,raw[:200],time.time()-t
def host_port(url,default_host,port):
    """Compose service names are not resolvable from the host; map them to the published port."""
    u=urlparse(url);return (default_host,port) if (u.hostname or '') in {'postgres','qdrant','redis','api'} else (u.hostname or default_host,u.port or port)
def tcp(host,port,send=None,timeout=2):
    try:
        with socket.create_connection((host,port),timeout=timeout) as s:
            if send:s.sendall(send);return s.recv(64)
            return b'ok'
    except OSError:return None

def main(argv=None):
    ap=argparse.ArgumentParser(description='Stack diagnostics.');ap.add_argument('--web',action='store_true',help='also run a smoke WEB search (sends one real query to the configured web providers; off by default)');args=ap.parse_args(argv)
    envp=ROOT/'.env';vals={}
    if envp.exists():vals={k:v for k,(_,v) in parse(envp.read_text()).items()}
    g=lambda k,d='':(vals.get(k) or os.environ.get(k) or d)
    api_port=int(g('API_PORT','8000'));api=f'http://127.0.0.1:{api_port}'
    # 1-4 platform
    rc,out,_=run('docker','info','--format','{{.ServerVersion}}')
    rec('OK' if rc==0 else 'FAIL','docker',f'daemon running (server {out})' if rc==0 else 'Docker is not running or not installed','start Docker Desktop / the docker service, then re-run')
    rc,out,_=run('docker','compose','version','--short')
    rec('OK' if rc==0 and out.lstrip('v')[:1]>='2' else 'FAIL','compose v2',out if rc==0 else 'docker compose plugin not found','install Docker Compose v2 (bundled with Docker Desktop)')
    rec('OK','cpu',f'{platform.system()} {platform.machine()}'+(' (arm64: images are multi-arch)' if platform.machine() in ('arm64','aarch64') else ''))
    free=shutil.disk_usage(ROOT).free/2**30;rec('OK' if free>=5 else 'FAIL','disk',f'{free:.0f} GB free','free at least 5 GB (images + model + vectors)')
    # 5 env file
    if not envp.exists():rec('FAIL','.env','missing','run ./rag up (it creates .env with generated secrets) or copy .env.example')
    else:
        mode=envp.stat().st_mode&0o777;ex=parse((ROOT/'.env.example').read_text());need=['APP_SECRET_KEY','ADMIN_PASSWORD','DATABASE_URL','QDRANT_URL','REDIS_URL']
        bad=[k for k in need if not vals.get(k) or vals[k] in {'replace-with-a-long-random-secret','change-this-on-first-run','change-me'}]
        rec('FAIL' if bad else 'OK','.env keys','placeholder or empty: '+', '.join(bad) if bad else 'required keys set (values not shown)','replace placeholders with random values (delete .env and run ./rag up to regenerate)' )
        rec('WARN' if vals.get('POSTGRES_PASSWORD') in ('','change-me',None) else 'OK','POSTGRES_PASSWORD','still the example default (the database is published on 127.0.0.1 only)' if vals.get('POSTGRES_PASSWORD') in ('','change-me',None) else 'set (value not shown)','set a random POSTGRES_PASSWORD before first start (changing it later needs ./rag reset or ALTER USER)')
        rec('OK' if len(vals.get('APP_SECRET_KEY','').encode())>=32 else 'FAIL','APP_SECRET_KEY','at least 32 bytes' if len(vals.get('APP_SECRET_KEY','').encode())>=32 else 'shorter than 32 bytes','use a random value of 32+ bytes')
        rec('OK' if mode&0o077==0 else 'WARN','.env permissions',oct(mode),'chmod 600 .env')
        miss=[k for k in ex if k not in vals];rec('OK' if not miss else 'WARN','.env completeness','all keys from .env.example present' if not miss else 'missing: '+', '.join(miss[:8]),'./rag up appends missing keys without touching existing values')
    # 6 ports / containers
    ports={'API_PORT':8000,'WEB_PORT':5173,'PG_PORT':5433,'QDRANT_PORT':6335,'REDIS_PORT':6380}
    for k,d in ports.items():
        p=int(g(k,str(d)));up=tcp('127.0.0.1',p) is not None;own=''
        if up:
            rc,o,_=run('lsof','-nP',f'-iTCP:{p}','-sTCP:LISTEN','-Fc')
            own=' (listener: '+(re.findall(r'^c(.+)$',o,re.M) or ['unknown'])[0]+')' if rc==0 and o else ''
        rec('OK' if up else 'WARN',f'port {k}={p}','listening'+own if up else 'nothing listening','start the stack: ./rag up (or ./rag dev)')
    rc,out,_=run('docker','compose','ps','--format','json')
    if rc==0 and out:
        rows=[json.loads(l) for l in out.splitlines() if l.startswith('{')];unhealthy=[r.get('Service') for r in rows if r.get('State')!='running' or r.get('Health') in ('unhealthy','starting')]
        rec('OK' if not unhealthy else 'WARN','containers',f"{len(rows)} running"+(f"; not healthy: {', '.join(unhealthy)}" if unhealthy else ''),'./rag logs <service>')
    else:rec('SKIP','containers','no compose containers (host mode, or not started)')
    # 7 API health
    st,j,dt=http('GET',api+'/health');api_up=st is not None
    if st is None:rec('FAIL','api /health',f'unreachable at {api}','./rag up  (or ./rag logs api)')
    else:
        comp=(j or {}).get('components',{}) if isinstance(j,dict) else {};bad=[f'{k}={v}' for k,v in comp.items() if v!='ok']
        rec('OK' if st==200 else 'FAIL','api /health',f'HTTP {st}, {dt*1000:.0f} ms; '+', '.join(f'{k}={v}' for k,v in comp.items()),'check the failing component: ./rag logs api')
        rec('OK' if comp.get('embedder')=='ok' else 'WARN','embedder warm-up',comp.get('embedder','unknown'),'the first search waits for the model to load; give the API ~10-60 s after start')
    # 8 qdrant dimension
    qh,qp=host_port(g('QDRANT_URL','http://localhost:6335'),'127.0.0.1',int(g('QDRANT_PORT','6335')));st,j,_=http('GET',f'http://{qh}:{qp}/collections/rag_chunks_v1')
    try:size=j['result']['config']['params']['vectors']['size']
    except (TypeError,KeyError):size=None
    want=int(g('EMBEDDING_DIMENSION','384'))
    rec('OK' if size==want else ('WARN' if st is None or st==404 else 'FAIL'),'qdrant collection',f'dimension {size} matches EMBEDDING_DIMENSION' if size==want else (f'dimension {size} != EMBEDDING_DIMENSION {want}' if size else 'collection not created yet (created on first API start)'),'recreate the collection and reindex after changing the embedding model')
    # 9 model cache
    model=g('EMBEDDING_MODEL','sentence-transformers/all-MiniLM-L6-v2').replace('/','--');cands=[Path(g('HF_HOME',str(Path.home()/'.cache/huggingface')))/'hub'/f'models--{model}']
    inimg=run('docker','compose','exec','-T','api','sh','-c',f'ls -d /opt/models/hub/models--{model} 2>/dev/null')[1] if shutil.which('docker') else ''
    rec('OK' if inimg or any(c.exists() for c in cands) else 'WARN','embedding model cache','baked into the api image' if inimg else ('cached locally' if any(c.exists() for c in cands) else 'not found'),'the first start downloads it (needs internet); the Docker image bakes it in at build time')
    # 10 redis
    rh,rp=host_port(g('REDIS_URL','redis://localhost:6380/0'),'127.0.0.1',int(g('REDIS_PORT','6380')));pong=tcp(rh,rp,b'PING\r\n')
    rec('OK' if pong and pong.startswith(b'+PONG') else 'WARN','redis cache',f'PING ok at {rh}:{rp}' if pong and pong.startswith(b'+PONG') else 'unreachable (web/overview caches fall back to per-process memory; indexing needs Redis)','start redis: ./rag up')
    # 11 Jev
    key=g('TYPESAFE_API_KEY')
    if not key:rec('WARN','jev','no TYPESAFE_API_KEY: search works (BM25 + vectors) but without rerank, routing and overview grounding','set TYPESAFE_API_KEY in .env or the admin console')
    else:
        st,j,dt=http('POST',g('JEV_BASE_URL','https://api.typesafe.ai').rstrip('/')+'/v1/systemone',{'Authorization':f'Bearer {key}'},{'model':g('JEV_MODEL','jev-latest'),'state':'ping','questions':{'ok':{'type':'noul','instructions':'Is this a ping?','criteria':{'true':'yes','false':'no'}}}})
        rec('OK' if st==200 else 'FAIL','jev',f'HTTP {st}, {dt*1000:.0f} ms' ,'check TYPESAFE_API_KEY, JEV_BASE_URL and network access')
    # 12 LLM
    base=g('OVERVIEW_LLM_BASE_URL') or g('LLM_BASE_URL')
    if not base:rec('WARN','llm','no LLM_BASE_URL: the optional AI Overview is unavailable (results are unaffected)','set LLM_BASE_URL/LLM_MODEL (and OVERVIEW_LLM_* for a faster model)')
    else:
        k=g('OVERVIEW_LLM_API_KEY') or g('LLM_API_KEY');st,j,dt=http('GET',base.rstrip('/')+'/models',{'Authorization':f'Bearer {k}'} if k else None)
        rec('OK' if st==200 else 'WARN','llm',f'HTTP {st}, {dt*1000:.0f} ms (models endpoint)','check the base URL / key; the overview shows "unavailable" instead of failing search')
    # 13-16 authenticated smoke
    pw=g('ADMIN_PASSWORD');tok=None
    if pw and api_up:
        st2,j2,_=http('POST',api+'/auth/login',body={'username':g('ADMIN_USERNAME','admin'),'password':pw});tok=j2.get('access_token') if isinstance(j2,dict) else None
        rec('OK' if tok else 'FAIL','admin login','ok' if tok else f'HTTP {st2}','ADMIN_PASSWORD is only applied when the admin user is first created; reset with ./rag reset or create another admin')
    if tok:
        h={'Authorization':'Bearer '+tok};st,docs,_=http('GET',api+'/admin/documents',h)
        n=len([d for d in docs if d.get('status')=='ready']) if isinstance(docs,list) else 0
        rec('OK' if n else 'WARN','sample corpus',f'{n} ready documents' if n else 'no ready documents','./rag seed')
        st,j,dt=http('POST',api+'/search',h,{'query':'parental leave policy','mode':'local','limit':3},timeout=60)
        if st==200:
            tm=', '.join(f'{k}={v:.0f}' for k,v in j['timings'].items() if v is not None);deg=j.get('degraded_stages') or [];jf=(j.get('usage',{}).get('jev') or {}).get('failed',0)
            fb=str(j.get('reranker','')).endswith('fallback') and bool(key);bad=[d for d in deg if d.startswith('jev')]
            msg=f"{len(j['results'])} results in {dt*1000:.0f} ms; timings {tm}; reranker={j['reranker']}"+(f"; degraded_stages={deg}" if deg else '')+(f"; jev requests failed={jf}" if jf else '')
            # A healthy direct Jev probe says nothing about the pooled client the API actually uses: a degraded search is a failure.
            if fb or bad or jf:rec('FAIL','smoke /search (DEGRADED)',msg,'Jev is reachable directly but the API is not getting usable answers (pool wedge, breaker open, timeouts): check ./rag logs api for "jev call failed" lines, then restart the api container')
            elif deg:rec('WARN','smoke /search',msg,'a stage degraded; see degraded_stages')
            else:rec('OK','smoke /search',msg)
        else:rec('FAIL','smoke /search',f'HTTP {st}','./rag logs api')
        st,cfg,_=http('GET',api+'/admin/config',h)
        if isinstance(cfg,dict) and cfg.get('jev_breaker') in ('open','half_open'):rec('FAIL','jev circuit breaker',f"{cfg['jev_breaker']} (the API stopped calling Jev after repeated failures)",'check ./rag logs api for "jev call failed" lines; it re-probes automatically after ~10 s')
        if isinstance(cfg,dict) and cfg.get('web_search_enabled'):
            rec('OK','web providers',f"{cfg.get('web_providers')}; breakers {cfg.get('web_breakers')}")
            if args.web:
                st,j,dt=http('POST',api+'/search',h,{'query':'what is the capital of australia','mode':'web','limit':3},timeout=30)
                rec('OK' if st==200 and j.get('results') else 'WARN','smoke web',f"HTTP {st}, {len(j.get('results',[])) if isinstance(j,dict) else 0} results in {dt*1000:.0f} ms"+(f"; degraded {j.get('degraded_stages')}" if isinstance(j,dict) and j.get('degraded_stages') else ''),'the provider may be rate limited; see breaker state above')
            else:rec('SKIP','smoke web','not run: it would send a real query to the web providers (pass --web to run it)')
        else:rec('SKIP','web search','disabled (private default)')
    fails=R.count('FAIL');print(f"\n{R.count('OK')} ok, {R.count('WARN')} warn, {fails} fail");return 1 if fails else 0
if __name__=='__main__':sys.exit(main())
