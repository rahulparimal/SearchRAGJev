"""Pluggable embedding, vector-store and OpenAI-compatible generation adapters."""
import asyncio,hashlib,json,logging,os,queue,threading,time,httpx
from concurrent.futures import Future
from ahttp import send
from urlcheck import safe_provider_url
log=logging.getLogger('rag.llm')
from collections import OrderedDict
from qdrant_client import QdrantClient,models
from sentence_transformers import SentenceTransformer,CrossEncoder

class LocalEmbedder:
 def __init__(self,model_name=None,dimension=None):self.name=model_name or os.getenv('EMBEDDING_MODEL','sentence-transformers/all-MiniLM-L6-v2');self.dimension=dimension or int(os.getenv('EMBEDDING_DIMENSION','384'));self.model=None;self.lock=threading.Lock();self.qcache=OrderedDict();self.ready=False;self._q=queue.Queue();self._worker=None;self._wlock=threading.Lock()
 def _load(self):
  with self.lock:
   if self.model is None:
    m=SentenceTransformer(self.name,device=os.getenv('EMBEDDING_DEVICE','cpu'));actual=m.get_sentence_embedding_dimension()
    if actual!=self.dimension:raise ValueError(f'Configured embedding dimension {self.dimension} does not match model dimension {actual}')
    self.model=m
  return self.model
 def encode(self,texts):
  return self._load().encode(texts,normalize_embeddings=True,show_progress_bar=False).tolist()
 def warm(self):
  try:
   for n in (1,4,16,32):self.encode(['warm up the embedding model for query batches']*n)  # first batches of each size are slow on some devices (kernel setup): pay for them at startup
  finally:self.ready=self.model is not None  # `ready` gates the dense leg (app.leg_dense): set it whenever the model actually loaded, even if a later warm-up batch failed, so a transient failure cannot leave dense search permanently off
 def _batcher(self):
  """Coalesces concurrent query embeddings into one forward pass (16 simultaneous searches cost ~100 ms together instead of ~16 x 30 ms contending for the CPU)."""
  while True:
   items=[self._q.get()];end=time.monotonic()+0.006
   while len(items)<32:
    left=end-time.monotonic()
    if left<=0:break
    try:items.append(self._q.get(timeout=left))
    except queue.Empty:break
   try:
    for (_,f),v in zip(items,self.encode([q for q,_ in items])):f.set_result(v)
   except Exception as e:
    for _,f in items:f.set_exception(e)
 def encode_query(self,q,size=2048,use_cache=True):
  """LRU over normalised queries (embeddings carry no document content, so sharing across users is safe); misses are micro-batched."""
  k=' '.join(q.lower().split())
  with self.lock:
   if use_cache and k in self.qcache:self.qcache.move_to_end(k);return self.qcache[k]
  with self._wlock:
   if self._worker is None:self._worker=threading.Thread(target=self._batcher,daemon=True,name='embed-batcher');self._worker.start()
  f=Future();self._q.put((q,f));v=f.result(timeout=60)
  with self.lock:
   self.qcache[k]=v
   while len(self.qcache)>size:self.qcache.popitem(last=False)
  return v

class LocalCrossEncoder:
 """Fallback local reranker for when Jev is disabled, unconfigured or (local-only mode) deliberately excluded: without this,
 those searches get no reranking at all beyond first-stage RRF. Runs entirely on-box (no external call), so it is safe for
 local-only/private mode. Lazy-loaded and warmed the same way as LocalEmbedder; call score() off the event loop (run_search)."""
 def __init__(self,model_name=None):self.name=model_name or os.getenv('LOCAL_RERANKER_MODEL','cross-encoder/ms-marco-MiniLM-L-6-v2');self.model=None;self.lock=threading.Lock();self.ready=False
 def _load(self):
  with self.lock:
   if self.model is None:self.model=CrossEncoder(self.name,device=os.getenv('EMBEDDING_DEVICE','cpu'))
  return self.model
 def warm(self):
  try:self._load().predict([('warm up','the cross-encoder reranker')])
  finally:self.ready=self.model is not None
 def score(self,query,texts):
  """-> list[float], one relevance score per text, same order as `texts`. Sync/CPU-bound: callers must run this off the event loop."""
  if not texts:return []
  return [float(s) for s in self._load().predict([(query,t) for t in texts])]

class QdrantVectors:
 def __init__(self,url=None,collection='rag_chunks_v1',dimension=None):
  self.collection=collection;self.dimension=dimension or int(os.getenv('EMBEDDING_DIMENSION','384'));self.client=QdrantClient(url=url or os.getenv('QDRANT_URL','http://localhost:6333'),timeout=10)
  if not self.client.collection_exists(self.collection):self.client.create_collection(self.collection,vectors_config=models.VectorParams(size=self.dimension,distance=models.Distance.COSINE))
  self.client.create_payload_index(self.collection,'doc_id',field_schema=models.PayloadSchemaType.KEYWORD,wait=True)
 def upsert(self,points):self.client.upsert(self.collection,points,wait=True)
 def search(self,vector,allowed_doc_ids,limit=30):
  if not allowed_doc_ids:return []
  filt=models.Filter(must=[models.FieldCondition(key='doc_id',match=models.MatchAny(any=allowed_doc_ids))])
  return self.client.query_points(self.collection,query=vector,query_filter=filt,limit=limit,with_payload=True).points
 def remove_doc_except(self,doc_id,keep_ids):
  cond=[models.FieldCondition(key='doc_id',match=models.MatchValue(value=doc_id))]
  self.client.delete(self.collection,points_selector=models.FilterSelector(filter=models.Filter(must=cond,must_not=[models.HasIdCondition(has_id=list(keep_ids))])),wait=True)
 def remove_doc(self,doc_id):
  selector=models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(key='doc_id',match=models.MatchValue(value=doc_id))]))
  self.client.delete(self.collection,points_selector=selector,wait=True)

_ov={'loop':None,'client':None}
def _ov_client():
 loop=asyncio.get_running_loop()
 if _ov['loop'] is not loop:_ov.update(loop=loop,client=httpx.AsyncClient(limits=httpx.Limits(max_connections=64,max_keepalive_connections=32,keepalive_expiry=60)))
 return _ov['client']
def reset_llm_client():_ov.update(loop=None,client=None)

def resolve_llm(g,tiers):
 """tiers: [(base_env,key_env,model_env), ...] tried in order; base/key/model are taken together from the SAME tier so a
 key is never paired with a different provider's base URL (the same rule as direct._cfg and OverviewGenerator._cfg).
 -> (base_url_or_None, key, model)."""
 for be,ke,me in tiers:
  b=(g(be,be) or '').strip()
  if b:return safe_provider_url(b),(g(ke,ke) or ''),(g(me,me) or '')
 return None,'',''

class ChatGenerator:
 """General OpenAI-compatible chat call over its own async httpx pool (no worker threads). Same retry/timeout shape as
 OverviewGenerator.agenerate (total budget, first-attempt cap, one retry on timeout/429/5xx honouring Retry-After) but
 config-prefix-driven so a new feature (e.g. Deep Research) doesn't re-implement this a third time. `prefix` e.g.
 'DEEP_RESEARCH_LLM' reads {prefix}_BASE_URL/API_KEY/MODEL as its own tier before falling back to `fallback_tiers`."""
 def __init__(self,setting_reader,prefix,fallback_tiers=(),total_s_env=None,attempt_s_env=None,max_tokens_env=None,extra_body_env=None,default_total_s=60,default_attempt_s=20,default_max_tokens=1200,temperature=0.2):
  self.setting=setting_reader;self.prefix=prefix;self.fallback_tiers=fallback_tiers
  self.total_s_env=total_s_env or f'{prefix}_TIMEOUT_S';self.attempt_s_env=attempt_s_env or f'{prefix}_ATTEMPT_S'
  self.max_tokens_env=max_tokens_env or f'{prefix}_MAX_TOKENS';self.extra_body_env=extra_body_env or f'{prefix}_EXTRA_BODY'
  self.default_total_s=default_total_s;self.default_attempt_s=default_attempt_s;self.default_max_tokens=default_max_tokens;self.temperature=temperature
 def _cfg(self):
  return resolve_llm(self.setting,[(f'{self.prefix}_BASE_URL',f'{self.prefix}_API_KEY',f'{self.prefix}_MODEL'),*self.fallback_tiers])
 def configured(self):b,_,m=self._cfg();return bool(b and m)
 @property
 def model(self):return self._cfg()[2]
 @property
 def base_url(self):return self._cfg()[0]
 @property
 def endpoint_id(self):return hashlib.sha256((self.base_url or '').encode()).hexdigest()[:12]
 async def agenerate(self,system,user,max_tokens=None,total_s=None,attempt_s=None):
  g=self.setting;base,key,model=self._cfg()
  if not base or not model:return ''
  total=total_s or float(g(self.total_s_env,self.total_s_env) or self.default_total_s)
  first=attempt_s or float(g(self.attempt_s_env,self.attempt_s_env) or self.default_attempt_s);end=time.monotonic()+total
  body={'model':model,'messages':[{'role':'system','content':system},{'role':'user','content':user}],'temperature':self.temperature,'max_tokens':max_tokens or int(g(self.max_tokens_env,self.max_tokens_env) or self.default_max_tokens)}
  try:body.update(json.loads(g(self.extra_body_env,self.extra_body_env) or '{}'))
  except (ValueError,TypeError):pass
  hdr={'Authorization':f'Bearer {key}'} if key else {};last=None
  for attempt in (0,1):
   left=end-time.monotonic()
   if left<=0.5:break
   try:r=await send(_ov_client(),'POST',base+'/chat/completions',min(left,first) if attempt==0 else left,headers=hdr,json=body)
   except (asyncio.TimeoutError,httpx.TimeoutException) as e:last=e;log.warning('%s llm attempt %d timed out',self.prefix,attempt+1);continue
   except httpx.HTTPError as e:last=e;log.warning('%s llm attempt %d failed reason=%s',self.prefix,attempt+1,type(e).__name__);continue
   if r.status_code in (429,500,502,503,504) and attempt==0:
    last=httpx.HTTPStatusError(str(r.status_code),request=r.request,response=r);log.warning('%s llm attempt 1 status=%d',self.prefix,r.status_code)
    try:wait=float(r.headers.get('retry-after') or 0) or (1.5 if r.status_code==429 else 0.3)
    except ValueError:wait=1.5
    await asyncio.sleep(max(0.0,min(wait,end-time.monotonic()-1.0)));continue
   r.raise_for_status();return (r.json()['choices'][0]['message'].get('content') or '').strip()
  raise last or asyncio.TimeoutError()
 def generate(self,system,user):
  """Sync wrapper (scripts/tests only): the API uses agenerate."""
  return asyncio.run(self.agenerate(system,user))

class OverviewGenerator:
 """OpenAI-compatible chat call for the AI Overview over its own async httpx pool (no worker threads, so slow overviews can never starve
 /search). OVERVIEW_LLM_* fall back to LLM_*; the LLM key is used for the overview endpoint ONLY when both resolve to the same base URL
 (a different overview host never receives another provider's key). Base URLs are validated (http/https, no credentials, no link-local).
 Total budget OVERVIEW_TIMEOUT_S; one retry on timeout/429/5xx when time remains (attempt 1 is capped at OVERVIEW_ATTEMPT_S so a stuck call
 leaves room for the retry). Raises on provider failure; returns '' when nothing is configured."""
 def __init__(self,setting_reader):self.setting=setting_reader
 def _cfg(self):
  g=self.setting;ob=(g('OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_BASE_URL') or '').strip();lb=(g('LLM_BASE_URL','LLM_BASE_URL') or '').strip()
  same=not ob or ob.rstrip('/')==lb.rstrip('/')
  key=g('OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_API_KEY') or (g('LLM_API_KEY','LLM_API_KEY') if same else '')
  return safe_provider_url(ob or lb),key,g('OVERVIEW_LLM_MODEL','OVERVIEW_LLM_MODEL') or g('LLM_MODEL','LLM_MODEL')
 def configured(self):b,_,m=self._cfg();return bool(b and m)
 @property
 def model(self):return self._cfg()[2]
 @property
 def base_url(self):return self._cfg()[0]
 @property
 def endpoint_id(self):return hashlib.sha256(self.base_url.encode()).hexdigest()[:12]  # part of the overview cache key
 async def agenerate(self,system,user):
  g=self.setting;base,key,model=self._cfg()
  if not base or not model:return ''
  total=float(g('OVERVIEW_TIMEOUT_S','OVERVIEW_TIMEOUT_S') or 12);first=float(g('OVERVIEW_ATTEMPT_S','OVERVIEW_ATTEMPT_S') or 7);end=time.monotonic()+total
  body={'model':model,'messages':[{'role':'system','content':system},{'role':'user','content':user}],'temperature':0.2,'max_tokens':int(g('OVERVIEW_MAX_TOKENS','OVERVIEW_MAX_TOKENS') or 350)}
  try:body.update(json.loads(g('OVERVIEW_EXTRA_BODY','OVERVIEW_EXTRA_BODY') or '{}'))  # e.g. a provider's "disable reasoning" switch
  except (ValueError,TypeError):pass
  hdr={'Authorization':f'Bearer {key}'} if key else {};last=None
  for attempt in (0,1):
   left=end-time.monotonic()
   if left<=0.5:break
   try:r=await send(_ov_client(),'POST',base+'/chat/completions',min(left,first) if attempt==0 else left,headers=hdr,json=body)
   except (asyncio.TimeoutError,httpx.TimeoutException) as e:last=e;log.warning('overview llm attempt %d timed out',attempt+1);continue
   except httpx.HTTPError as e:last=e;log.warning('overview llm attempt %d failed reason=%s',attempt+1,type(e).__name__);continue
   if r.status_code in (429,500,502,503,504) and attempt==0:
    last=httpx.HTTPStatusError(str(r.status_code),request=r.request,response=r);log.warning('overview llm attempt 1 status=%d',r.status_code)
    try:wait=float(r.headers.get('retry-after') or 0) or (1.5 if r.status_code==429 else 0.3)  # honour Retry-After; a 429 retried at once only gets another 429
    except ValueError:wait=1.5
    await asyncio.sleep(max(0.0,min(wait,end-time.monotonic()-1.0)));continue
   r.raise_for_status();return (r.json()['choices'][0]['message'].get('content') or '').strip()
  raise last or asyncio.TimeoutError()
 def generate(self,system,user):
  """Sync wrapper (scripts/tests only): the API uses agenerate."""
  return asyncio.run(self.agenerate(system,user))
