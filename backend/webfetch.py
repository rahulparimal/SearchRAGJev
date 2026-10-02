"""Deep Research page fetcher (Phase B): fetches the full page for a search result instead of relying on the provider's
short snippet, which is the main reason citations in the Phase A (snippets-only) report were often flagged as unverified
-- a 150-300 character snippet rarely contains enough to ground a detailed claim, even when the full page would.

This is a DIFFERENT, STRICTER trust boundary than urlcheck.validate_provider_url: that function deliberately allows
private/loopback hosts because it vets ADMIN-CONFIGURED provider endpoints (local models, docker service names). This
module fetches URLs that came back from a THIRD-PARTY web search result, which is untrusted input that could point
anywhere, so it requires every resolved IP to be globally routable (no private/loopback/link-local/metadata ranges) --
checked by resolving DNS ourselves before connecting.

Known residual risk (documented, not fully closed): this is a resolve-then-connect check, not a resolve-and-pin-the-
connection-to-that-IP check, so a DNS answer that changes between the check and httpx's own connect (DNS rebinding) is
not caught. Closing that gap needs a custom httpx transport that connects to a specific pre-resolved IP; given this
fetches read-only text from public web search results (no credentials, no state-changing requests), the residual risk is
a server-side-request-forgery read against an internal address chosen by a narrow TOCTOU race, not credential theft.
DEEP_RESEARCH_FETCH_ALLOWLIST (comma-separated hosts/.suffixes) closes it entirely for deployments that want to restrict
fetching to a known set of domains."""
import asyncio,html.parser,ipaddress,logging,os,socket
from urllib.parse import urlparse,urljoin
import httpx
from ahttp import send
from web_search import clean_text
log=logging.getLogger('rag.webfetch')

MAX_PAGES=int(os.getenv('DEEP_RESEARCH_MAX_PAGES','20'))
PAGE_MAX_BYTES=int(os.getenv('DEEP_RESEARCH_PAGE_MAX_BYTES',str(1_500_000)))
PAGE_TIMEOUT_S=float(os.getenv('DEEP_RESEARCH_PAGE_TIMEOUT_S','6'))
PAGE_TEXT_CHARS=int(os.getenv('DEEP_RESEARCH_PAGE_TEXT_CHARS','2500'))
PAGE_EXTRACT_CHARS=int(os.getenv('DEEP_RESEARCH_PAGE_EXTRACT_CHARS','20000'))  # kept per page so the relevant part can be chosen, not just the intro
MAX_REDIRECTS=3
ALLOWED_TYPES=('text/html','text/plain','application/xhtml+xml')

_st={'loop':None,'client':None}
def _client():
    loop=asyncio.get_running_loop()
    if _st['loop'] is not loop:_st.update(loop=loop,client=httpx.AsyncClient(follow_redirects=False,limits=httpx.Limits(max_connections=16,max_keepalive_connections=8,keepalive_expiry=30)))
    return _st['client']
def reset():_st.update(loop=None,client=None)

def _is_global_host(hostname:str)->bool:
    """Resolve `hostname` and require EVERY returned address to be globally routable. Blocks private/loopback/link-local/
    multicast/reserved/unspecified ranges and the cloud metadata address (169.254.169.254 is link-local, already covered,
    kept explicit for clarity). Resolution itself runs in a thread (socket.getaddrinfo is blocking)."""
    try:
        infos=socket.getaddrinfo(hostname,None)
    except socket.gaierror:
        return False
    if not infos:return False
    for info in infos:
        addr=info[4][0]
        try:ip=ipaddress.ip_address(addr.split('%')[0])
        except ValueError:return False
        if getattr(ip,'ipv4_mapped',None):ip=ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified:
            return False
    return True

def _allowlisted(hostname:str)->bool:
    raw=os.getenv('DEEP_RESEARCH_FETCH_ALLOWLIST','').strip()
    if not raw:return True
    allow=[x.strip().lower() for x in raw.split(',') if x.strip()]
    h=hostname.lower().rstrip('.')
    return any(h==a.lstrip('.') or h.endswith('.'+a.lstrip('.')) for a in allow)

class _TextExtractor(html.parser.HTMLParser):
    """Minimal, dependency-free text extraction: drops script/style/nav/footer/header/aside content, keeps everything else
    as flat text. Not a layout-aware readability algorithm -- good enough to turn a page into groundable prose."""
    SKIP={'script','style','nav','footer','header','aside','noscript','svg','form'}
    def __init__(self):
        super().__init__(convert_charrefs=True);self.skip_depth=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in self.SKIP:self.skip_depth+=1
    def handle_endtag(self,tag):
        if tag in self.SKIP and self.skip_depth>0:self.skip_depth-=1
    def handle_data(self,data):
        if self.skip_depth==0 and data.strip():self.parts.append(data.strip())
    def text(self):return ' '.join(self.parts)

async def fetch_page(url:str,deadline_s:float|None=None)->str|None:
    """-> extracted page text (<=PAGE_TEXT_CHARS), or None on any failure/rejection (callers fall back to the snippet they
    already have -- a failed page fetch is never fatal to the research run)."""
    budget=min(deadline_s,PAGE_TIMEOUT_S) if deadline_s is not None else PAGE_TIMEOUT_S
    try:
        for _ in range(MAX_REDIRECTS+1):
            u=urlparse(url)
            if u.scheme not in ('http','https') or not u.hostname:return None
            if not _allowlisted(u.hostname):log.info('webfetch host not allowlisted host=%s',u.hostname);return None
            ok=await asyncio.get_running_loop().run_in_executor(None,_is_global_host,u.hostname)
            if not ok:log.info('webfetch host rejected (not globally routable) host=%s',u.hostname);return None
            client=_client()
            try:resp=await send(client,'GET',url,budget,headers={'User-Agent':'Mozilla/5.0 (compatible; RAGSearchJev-DeepResearch/1.0)'})
            except (asyncio.TimeoutError,httpx.HTTPError) as e:log.info('webfetch failed url_host=%s reason=%s',u.hostname,type(e).__name__);return None
            if resp.status_code in (301,302,303,307,308):
                loc=resp.headers.get('location')
                await resp.aclose()
                if not loc:return None
                url=urljoin(url,loc);continue
            if resp.status_code>=400:await resp.aclose();return None
            ctype=(resp.headers.get('content-type') or '').split(';')[0].strip().lower()
            if ctype and ctype not in ALLOWED_TYPES:await resp.aclose();return None
            body=b''
            try:
                async for chunk in resp.aiter_bytes():
                    body+=chunk
                    if len(body)>PAGE_MAX_BYTES:break
            finally:await resp.aclose()
            text=body.decode(resp.encoding or 'utf-8',errors='replace')
            parser=_TextExtractor()
            try:parser.feed(text)
            except Exception:return None
            extracted=clean_text(parser.text(),PAGE_EXTRACT_CHARS)
            return extracted or None
        return None  # too many redirects
    except Exception:
        log.warning('webfetch unexpected error url=%s',url,exc_info=True);return None

async def fetch_pages(urls:list[str],deadline_s:float)->dict[str,str]:
    """Fetch up to MAX_PAGES URLs concurrently (bounded), within the shared deadline. -> {url: text} for successes only."""
    sem=asyncio.Semaphore(4)
    async def one(u):
        async with sem:
            t=await fetch_page(u,deadline_s)
        return u,t
    results=await asyncio.gather(*(one(u) for u in urls[:MAX_PAGES]))
    return {u:t for u,t in results if t}

def relevant_excerpt(text:str,query:str,chars:int,win:int=500)->str:
    """The parts of `text` that best match `query` (term overlap), up to ~`chars`, joined in original order. Pure function, no LLM.
    Pages open with navigation and intros; claims are usually supported deeper in. Short texts are returned unchanged."""
    from bm25 import content_terms,tokenize
    text=text or ''
    if len(text)<=chars:return text
    q=content_terms(query)
    if not q:return text[:chars]
    step=max(100,win//2);wins=[(i,text[i:i+win]) for i in range(0,max(1,len(text)-win+step),step)]
    scored=sorted(((sum(1 for t in set(tokenize(w)) if t in q),-i,i) for i,w in wins),reverse=True)
    picked=[];used=0
    for sc,_,i in scored:
        if used+win>chars+win//2:break
        if any(abs(i-j)<win for j in picked):continue
        picked.append(i);used+=win
        if used>=chars:break
    if not picked:return text[:chars]
    pad=win//4  # context on both sides so a matching sentence is not cut at the window edge
    return ' ... '.join(text[max(0,i-pad):i+win+pad] for i in sorted(picked))
