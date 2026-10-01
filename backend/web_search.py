"""Web search for generic questions. Only the user's query leaves the system, never document text. Results are untrusted third-party
text: cleaned, de-duplicated, BM25-ranked (always on) and later reranked by Jev; web hits are never embedded or vector-searched.
Providers form an ordered chain (optional Brave API / SearXNG, then ddgs with pinned engines) with hedging, a circuit breaker and a cache."""
import asyncio,hashlib,json,logging,os,re,time
from datetime import datetime,timezone
from urllib.parse import urlparse,urlunparse,parse_qsl,urlencode
from bm25 import Bm25Index,tokenize
from cache import cache
from urlcheck import safe_provider_url
log=logging.getLogger('rag.web')

CITATION=re.compile(r'\[S\d+\]')
MARKER=re.compile(r'<<<.*?>>>|<<<|>>>')
def clean_text(text:str,limit:int=600)->str:
    """Strip look-alike citation markers and prompt delimiters so web text cannot forge [S#] references or source boundaries."""
    return re.sub(r'\s+',' ',MARKER.sub('',CITATION.sub('',text or ''))).strip()[:limit]
def safe_url(url:str)->str:
    """Only absolute http(s) URLs are kept; javascript:, data:, file: etc. are dropped."""
    try:u=urlparse((url or '').strip())
    except ValueError:return ''
    return u.geturl() if u.scheme in {'http','https'} and u.netloc else ''
def domain_of(url:str)->str:
    h=(urlparse(url).hostname or '').lower();return h[4:] if h.startswith('www.') else h
def canonical_url(url:str)->str:
    """Lowercase host, drop www., utm_*/fbclid/gclid params and fragment, trailing slash."""
    u=urlparse(url);q=urlencode([(k,v) for k,v in parse_qsl(u.query) if not k.lower().startswith('utm_') and k.lower() not in {'fbclid','gclid','ref'}])
    return urlunparse((u.scheme.lower(),domain_of(url)+(f':{u.port}' if u.port else ''),u.path.rstrip('/') or '/','',q,''))

PRIOR_DEFAULT=0.4
DEFAULT_PRIORS={'.gov':1.0,'.edu':0.9,'wikipedia.org':0.9,'python.org':1.0,'mozilla.org':0.9,'ietf.org':1.0,'w3.org':1.0,'rfc-editor.org':1.0,'github.com':0.7,'stackoverflow.com':0.7,'who.int':1.0,'nih.gov':1.0,'arxiv.org':0.8,
 'pinterest.com':0.0,'quora.com':0.15,'ehow.com':0.0,'answers.com':0.1}
def priors():
    p=dict(DEFAULT_PRIORS)
    try:p.update({str(k).lower():float(v) for k,v in json.loads(os.getenv('WEB_DOMAIN_PRIORS') or '{}').items()})
    except (ValueError,TypeError,AttributeError):pass
    return p
def domain_prior(domain:str,table=None)->float:
    t=table if table is not None else DEFAULT_PRIORS
    return max((v for k,v in t.items() if domain==k.lstrip('.') or domain.endswith('.'+k.lstrip('.'))),default=PRIOR_DEFAULT)

ENGINE_HOSTS={'bing.com','r.bing.com','search.yahoo.com','r.search.yahoo.com','duckduckgo.com','google.com','search.brave.com','ecosia.org','startpage.com','yandex.com','yandex.ru','baidu.com','search.aol.com'}
REDIRECTOR_SUFFIXES=('doubleclick.net','googleadservices.com','googlesyndication.com','clickserve.dartsearch.net','click.linksynergy.com','go.redirectingat.com')
def engine_link(url):
    """True for results that are not real sources: any result hosted on a search engine (their /search, /aclk and Bing /aclick ads, redirect
    and image pages are echoed back by some engines) or on a known ad redirector."""
    h=domain_of(url)
    return h in ENGINE_HOSTS or any(h==x or h.endswith('.'+x) for x in REDIRECTOR_SUFFIXES)
def _published_recent(s):
    try:d=datetime.fromisoformat(str(s).replace('Z','+00:00'));d=d if d.tzinfo else d.replace(tzinfo=timezone.utc);return (datetime.now(timezone.utc)-d).days<=365
    except (ValueError,TypeError):return False
def rank_hits(query:str,rows:list[dict],limit:int=8,fresh:bool=False,table=None)->list[dict]:
    """clean -> http(s) only -> canonical-URL + near-duplicate-title de-dup -> max 2 per domain -> first-stage score: BM25 over title+snippet
    (always on) fused with the engine's own order (weight 2:1; BM25 alone measured worse than engine order on recorded fixtures) plus a small
    domain-prior term. Hits carry bm25, prior and first (the fused first-stage score used by the Jev blend)."""
    out=[];seen_u=set();seen_t=[];per={}
    for r in rows:
        u=safe_url(r.get('url',''))
        if not u or engine_link(u):continue
        c=canonical_url(u);t=clean_text(r.get('title'),200);tt=set(tokenize(t));d=domain_of(u)
        if c in seen_u or any(tt and len(tt&s)/len(tt|s)>=0.9 for s in seen_t) or per.get(d,0)>=2:continue
        seen_u.add(c);seen_t.append(tt);per[d]=per.get(d,0)+1
        out.append({'title':t,'url':u,'text':clean_text(r.get('text')),'domain':d,'published':str(r.get('published') or ''),'prior':domain_prior(d,table)})
    if not out:return []
    bm=dict(Bm25Index([f"{r['title']} {r['text']}" for r in out]).search(query,len(out)))
    bm_rank={i:n for n,(i,_) in enumerate(sorted(bm.items(),key=lambda x:-x[1]))};miss=len(out)+20  # no query-term overlap: ranked below every BM25 hit
    def first(i):return 2/(60+i)+1/(60+bm_rank.get(i,miss))+0.02*out[i]['prior']+(0.003 if fresh and _published_recent(out[i]['published']) else 0.0)
    return [dict(out[i],bm25=round(bm.get(i,0.0),3),first=round(first(i),5)) for i in sorted(range(len(out)),key=lambda i:(first(i),-i),reverse=True)[:limit]]

# ---- providers (sync .fetch, run in worker threads by the hedged fetcher) ----
class DdgsProvider:
    """ddgs with pinned engines (WEB_DDGS_BACKENDS, default yahoo,bing: measured ~0.6 s vs ~2.4 s for bing alone) so the query goes only where
    the UI/admin text says; ddgs's default 'auto' fans out to Wikipedia plus random engines. Shared instance."""
    name='ddgs'
    def __init__(self,backends='yahoo,bing',timeout=3):self.backends=backends or 'yahoo,bing';self.timeout=timeout;self._d=None
    @property
    def engines(self):return [b.strip() for b in self.backends.split(',') if b.strip()]
    def fetch(self,query,limit,freshness=None):
        from ddgs import DDGS
        if self._d is None:self._d=DDGS(timeout=self.timeout)
        kw={'max_results':limit+4,'backend':self.backends}
        if freshness:kw['timelimit']='y'
        try:rows=self._d.text(query,**kw)
        except Exception as e:
            if 'no results' in str(e).lower():return []  # ddgs signals "nothing found" with an exception; that is an empty answer, not a provider failure
            raise
        return [{'title':r.get('title',''),'url':r.get('href',''),'text':r.get('body',''),'published':r.get('date','')} for r in rows]
DuckDuckGoSearch=DdgsProvider  # legacy name

class BraveApiProvider:
    name='brave_api';engines=['Brave Search API']
    def __init__(self,key,timeout=2.5):self.key=key;self.timeout=timeout
    def fetch(self,query,limit,freshness=None):
        import httpx
        p={'q':query,'count':min(20,limit+4)}
        if freshness:p['freshness']='py'
        r=httpx.get('https://api.search.brave.com/res/v1/web/search',params=p,headers={'X-Subscription-Token':self.key,'Accept':'application/json'},timeout=self.timeout);r.raise_for_status()
        return [{'title':x.get('title',''),'url':x.get('url',''),'text':x.get('description',''),'published':x.get('page_age','')} for x in (r.json().get('web') or {}).get('results',[])]

class SearxngProvider:
    name='searxng'
    def __init__(self,url,timeout=2.5):self.url=url.rstrip('/');self.timeout=timeout;self.engines=['SearXNG (self-hosted)']
    def fetch(self,query,limit,freshness=None):
        import httpx
        r=httpx.get(self.url+'/search',params={'q':query,'format':'json',**({'time_range':'year'} if freshness else {})},timeout=self.timeout);r.raise_for_status()
        return [{'title':x.get('title',''),'url':x.get('url',''),'text':x.get('content',''),'published':x.get('publishedDate') or ''} for x in r.json().get('results',[])]

def build_chain(get=os.getenv):
    """Ordered providers from WEB_PROVIDERS (default 'ddgs', or 'brave_api,ddgs' when BRAVE_API_KEY is set). Unconfigured ones are skipped; ddgs is always a last resort."""
    brave=get('BRAVE_API_KEY') or '';searx=get('SEARXNG_URL') or ''
    if searx:
        searx=safe_provider_url(searx)
        if not searx:log.error('SEARXNG_URL rejected by URL validation; searxng provider skipped')
    order=[x.strip() for x in (get('WEB_PROVIDERS') or ('brave_api,ddgs' if brave else 'ddgs')).split(',') if x.strip()];chain=[]
    for n in order:
        if n=='brave_api' and brave:chain.append(BraveApiProvider(brave))
        elif n=='searxng' and searx:chain.append(SearxngProvider(searx))
        elif n=='ddgs':chain.append(DdgsProvider(get('WEB_DDGS_BACKENDS') or 'yahoo,bing'))
    return chain or [DdgsProvider(get('WEB_DDGS_BACKENDS') or 'yahoo,bing')]
def describe_chain(chain):
    names=[];[names.extend(p.engines) for p in chain]
    return ', '.join(dict.fromkeys(n.capitalize() if n.islower() else n for n in names))

class CircuitBreaker:
    """3 failures within 60 s open the breaker for 120 s; then one half-open probe decides."""
    def __init__(self,threshold=3,window=60.0,cooldown=120.0,clock=time.monotonic):self.th=threshold;self.win=window;self.cool=cooldown;self.clock=clock;self.fails=[];self.open_until=0.0;self.probing=False
    def state(self):
        if not self.open_until:return 'closed'
        return 'open' if self.clock()<self.open_until else 'half_open'
    def allow(self):
        s=self.state()
        if s=='closed':return True
        if s=='open' or self.probing:return False
        self.probing=True;return True
    def ok(self):self.fails=[];self.open_until=0.0;self.probing=False
    def fail(self):
        now=self.clock()
        if self.probing:self.probing=False;self.open_until=now+self.cool;return
        self.fails=[t for t in self.fails if now-t<self.win]+[now]
        if len(self.fails)>=self.th:self.open_until=now+self.cool;self.fails=[]

class WebUnavailable(Exception):
    """Errors or timeouts only (never an empty answer). `sent` lists providers a request was actually sent to (empty when the breaker was open)."""
    def __init__(self,reason,sent=()):super().__init__(reason);self.reason=str(reason);self.sent=list(sent)
async def hedged_fetch(chain,breakers,query,limit,freshness=None,hedge_ms=700,timeout_ms=3000):
    """Call the first allowed provider; start the next after hedge_ms (or at once if the first fails); the first non-empty result wins, losers are
    abandoned. An empty answer is NOT a failure (the breaker is not touched; returns ([], meta{'empty':True})); only exceptions and the hard
    deadline count. Raises WebUnavailable on errors/timeouts. Returns (rows, meta{provider,hedged,sent})."""
    avail=[p for p in chain if breakers.setdefault(p.name,CircuitBreaker()).allow()]
    if not avail:raise WebUnavailable('breaker_open',[])
    end=time.monotonic()+timeout_ms/1000;pending={};nxt=0;hedged=False;retried=False;errored=set();empties=set();sent=[]  # a request counts once per provider toward its breaker, however many retries it made
    def launch():
        nonlocal nxt
        p=avail[nxt];nxt+=1;sent.append(p.name);pending[asyncio.ensure_future(asyncio.to_thread(p.fetch,query,limit,freshness))]=p
    launch()
    try:
        while pending:
            left=end-time.monotonic()
            if left<=0:break
            done,_=await asyncio.wait(list(pending),timeout=min(left,hedge_ms/1000) if nxt<len(avail) else left,return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                p=pending.pop(t)
                try:rows=t.result()
                except Exception as e:rows=None;errored.add(p.name);log.warning('web provider %s failed reason=%s',p.name,type(e).__name__)
                if rows:
                    breakers[p.name].ok()
                    for n in errored-{p.name}:breakers[n].fail()
                    return rows,{'provider':p.name,'hedged':hedged,'sent':list(dict.fromkeys(sent))}
                if rows is not None:empties.add(p.name)
            if nxt>=len(avail) and not pending and not retried and end-time.monotonic()>1.0:retried=True;avail.append(avail[0])  # one retry of a fast-failing/empty sole provider
            if nxt<len(avail) and (not done or not pending):hedged=hedged or not done;launch()
    finally:
        for t,p in pending.items():
            t.cancel()
            if time.monotonic()>=end:errored.add(p.name);log.warning('web provider %s timed out',p.name)
    sent=list(dict.fromkeys(sent))
    for n in errored:breakers[n].fail()
    for n in empties-errored:breakers[n].ok()
    if empties:return [],{'provider':sorted(empties)[0],'hedged':hedged,'empty':True,'sent':sent}
    raise WebUnavailable('timeout' if time.monotonic()>=end else 'failed',sent)

class WebSearcher:
    """Cache + chain + breakers. Cache holds only public, cleaned results under a hashed query key (no user identity)."""
    def __init__(self,chain=None,get=os.getenv):
        self.get=get;self.chain=chain or build_chain(get);self.breakers={}
    def sig(self):return '+'.join(p.name+(':'+p.backends if hasattr(p,'backends') else '') for p in self.chain)
    async def search(self,query,limit=8,freshness=None):
        """Returns (hits, meta{cache_hit,provider,hedged,fetch_ms,error}); hits are ranked by BM25. Never raises."""
        n=lambda k,d:int(self.get(k) or d)
        key=f"web:v1:{self.sig()}:{'f' if freshness else '-'}:{hashlib.sha256(' '.join(tokenize(query)).encode()).hexdigest()}"
        t=time.perf_counter();hit=await cache.aget(key)
        if hit is not None:return rank_hits(query,hit,limit,bool(freshness),priors()),{'cache_hit':True,'provider':'cache','hedged':False,'sent':[],'fetch_ms':(time.perf_counter()-t)*1000}
        try:rows,meta=await hedged_fetch(self.chain,self.breakers,query,max(limit,10),freshness,n('WEB_HEDGE_MS',700),n('WEB_TIMEOUT_MS',3000))
        except WebUnavailable as e:return [],{'cache_hit':False,'provider':None,'hedged':False,'error':e.reason,'sent':e.sent,'fetch_ms':(time.perf_counter()-t)*1000}
        rows=[r for r in rows if safe_url(r.get('url',''))]
        if rows:await cache.aset(key,rows,n('WEB_CACHE_TTL_S',900) if not freshness else min(120,n('WEB_CACHE_TTL_S',900)))
        return rank_hits(query,rows,limit,bool(freshness),priors()),dict(meta,cache_hit=False,fetch_ms=(time.perf_counter()-t)*1000)
    def breaker_states(self):return {p.name:self.breakers.get(p.name,CircuitBreaker()).state() for p in self.chain}

def web_search(query:str,limit:int=8,provider=None)->list[dict]:
    """Synchronous one-shot (tests/CLI): fetch from one provider and rank. Errors propagate so callers can report a degraded result."""
    return rank_hits(query,(provider or DdgsProvider()).fetch(query,max(limit*2,10)),limit,table=priors())
