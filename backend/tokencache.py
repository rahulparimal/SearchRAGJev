"""Token-saving cache for LLM-derived material (plans, reports, answers). One place for: key construction, encryption at rest,
single-flight de-duplication of identical in-flight work, TTLs per namespace, and hit/miss/tokens-saved counters for /health.
Backed by cache.py (Redis, in-process LRU fallback). See docs/TOKEN_EFFICIENCY.md."""
import asyncio,hashlib,json,os,re
from cache import cache
from execpool import run_ov

TTL={'plan':int(os.getenv('TOKENCACHE_PLAN_TTL_S','86400')),'report':int(os.getenv('DEEP_RESEARCH_CACHE_TTL_S','1800')),
     'route':int(os.getenv('TOKENCACHE_ROUTE_TTL_S','86400')),'answer':int(os.getenv('TOKENCACHE_ANSWER_TTL_S','900'))}
ENABLED=os.getenv('TOKENCACHE_ENABLED','true').strip().lower()!='false'
_stats={}  # namespace -> {'hit','miss','coalesced','tokens_saved'}
_inflight={}  # key -> asyncio.Future (single-flight; per event loop is fine, the API runs one)

def _s(ns):return _stats.setdefault(ns,{'hit':0,'miss':0,'coalesced':0,'tokens_saved':0})
def est_tokens(text):return max(1,len(text or '')//4)  # rough estimate (~4 chars/token); labelled an estimate wherever shown

def norm_query(q):
    """Casefold, collapse whitespace, drop trailing punctuation: 'What is  Dharma?' and 'what is dharma' share a key."""
    return re.sub(r'[\s]+',' ',(q or '').casefold()).strip(' ?!.')

def make_key(ns,*parts):
    """Namespaced, versioned, hashed key. Callers must put everything that changes the output in `parts`
    (model, endpoint, scope/user for ACL-dependent material, option flags)."""
    return f'tc:v1:{ns}:'+hashlib.sha256('\x1f'.join(str(p) for p in parts).encode()).hexdigest()

def _seal(obj):
    from config_store import key_fernet
    return {'enc':key_fernet().encrypt(json.dumps(obj,ensure_ascii=False).encode()).decode()}
def _unseal(v):
    if not isinstance(v,dict) or 'enc' not in v:return None
    try:
        from config_store import key_fernet
        return json.loads(key_fernet().decrypt(v['enc'].encode()).decode())
    except Exception:return None  # wrong key / tampered / old format: a miss, never an error

async def get(ns,key):
    if not ENABLED:return None
    hit=_unseal(await cache.aget(key,pool=run_ov))
    s=_s(ns)
    if hit is None:s['miss']+=1;return None
    s['hit']+=1;s['tokens_saved']+=hit.get('_tok',0);return hit['v']

async def put(ns,key,value,tokens=0,ttl=None):
    """Store a value. `tokens` = estimated LLM tokens a future hit avoids (for the savings counter)."""
    if not ENABLED or value is None:return
    await cache.aset(key,_seal({'v':value,'_tok':int(tokens)}),ttl or TTL.get(ns,900),pool=run_ov)

async def get_or_compute(ns,key,compute,tokens=0,ttl=None):
    """Return the cached value, else run `compute()` (async, -> value or None) exactly once even when identical requests
    arrive concurrently; followers wait for the leader instead of spending tokens twice. None results are never cached."""
    v=await get(ns,key)
    if v is not None:return v
    fut=_inflight.get(key)
    if fut is not None:
        _s(ns)['coalesced']+=1
        try:
            v=await asyncio.shield(fut)
            if v is not None:_s(ns)['tokens_saved']+=tokens;return v
        except Exception:pass  # leader failed: fall through and compute for ourselves
    fut=asyncio.get_running_loop().create_future();_inflight[key]=fut
    try:
        v=await compute();fut.set_result(v)
        if v is not None:await put(ns,key,v,tokens,ttl)
        return v
    except BaseException as e:
        if not fut.done():fut.set_exception(e)
        fut.exception()  # mark retrieved so an unawaited failure never logs "exception was never retrieved"
        raise
    finally:_inflight.pop(key,None)

def snapshot():
    """For /health: per-namespace counters plus an overall hit rate. tokens_saved is an ESTIMATE (chars/4), not billing data."""
    out={ns:dict(v) for ns,v in _stats.items()}
    h=sum(v['hit'] for v in _stats.values());m=sum(v['miss'] for v in _stats.values())
    return {'enabled':ENABLED,'namespaces':out,'hit_rate':round(h/(h+m),3) if h+m else None,'est_tokens_saved':sum(v['tokens_saved'] for v in _stats.values())}
def reset():_stats.clear();_inflight.clear()
