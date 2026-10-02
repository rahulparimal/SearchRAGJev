"""Quick answer: a direct, non-RAG chat completion streamed over SSE. No retrieval, no Jev, no embedder, no Postgres/Qdrant -- the only
dependency is the configured LLM provider, so this path is immune to every local-pipeline slowness cause (cold embedder load, Jev pool
contention, retrieval fan-out). It is NOT private: the query (and any client-held follow-up history) goes to the configured provider by
default, so callers must disable this entirely when the deployment is local-KB-only (see app.local_only_enforced)."""
import asyncio,hashlib,json,logging,os,re,time,httpx
import tokencache as tc
from urlcheck import safe_provider_url
log=logging.getLogger('rag.direct')

MAX_CONCURRENT=int(os.getenv('DIRECT_MAX_CONCURRENT','32'));_active=[0]
FIRST_TOKEN_S=float(os.getenv('DIRECT_FIRST_TOKEN_S','8'));TOTAL_S=float(os.getenv('DIRECT_TOTAL_S','60'))
SYSTEM=("You are a fast, concise general-knowledge assistant. Answer directly in plain text (light markdown such as short lists is fine). "
 "You have NOT been given any of the user's private documents or search results for this question -- if the question clearly depends on "
 "private/internal information you cannot know, say so briefly and suggest the user try Search instead. Say when you are not sure.")

CACHE_ENABLED=os.getenv('DIRECT_CACHE_ENABLED','false').strip().lower()=='true'  # off by default: a wrong answer would be frozen for the TTL
# Queries whose right answer depends on time or on the asker are never cached or served from cache.
_UNCACHEABLE=re.compile(r"\b(today|tonight|now|latest|current(ly)?|recent(ly)?|yesterday|tomorrow|this (week|month|year)|news|price|prices|weather|score|stock|my|mine|me|i|i'm|i've|we|our|us)\b|\b20\d\d\b")
def cacheable(query,history):return CACHE_ENABLED and not history and not _UNCACHEABLE.search((query or '').casefold())

_st={'loop':None,'client':None}
def _client():
 loop=asyncio.get_running_loop()
 if _st['loop'] is not loop:_st.update(loop=loop,client=httpx.AsyncClient(limits=httpx.Limits(max_connections=64,max_keepalive_connections=32,keepalive_expiry=90)))
 return _st['client']
def reset():_st.update(loop=None,client=None)

def _cfg(g):
 """Tier fallback DIRECT_LLM_* -> OVERVIEW_LLM_* -> LLM_*, each base/key/model taken together from the SAME tier so a key is never paired
 with a different provider's base URL (mirrors overview.OverviewGenerator's same-host rule)."""
 for bk,kk,mk in (('DIRECT_LLM_BASE_URL','DIRECT_LLM_API_KEY','DIRECT_LLM_MODEL'),('OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_MODEL'),('LLM_BASE_URL','LLM_API_KEY','LLM_MODEL')):
  b=(g(bk,bk) or '').strip()
  if b:return safe_provider_url(b),(g(kk,kk) or ''),(g(mk,mk) or '')
 return None,'',''
def configured(g):b,_,m=_cfg(g);return bool(b and m)
def model_name(g):return _cfg(g)[2]
def direct_status(g):
 """For /auth/me: never leaks the key, just whether Quick answers are available and which model answers. `g` is a setting(name,envname) reader."""
 try:b,_,m=_cfg(g)
 except Exception:b=m=None
 return {'available':bool(b and m),'model':m or None}

def _sse(name,data):return f'event: {name}\ndata: {json.dumps(data,ensure_ascii=False)}\n\n'

async def stream_answer(g,query,history=None):
 """Yields SSE strings: meta{model} -> token{t}* -> done{ttft_ms,total_ms} | error{reason}. Never raises."""
 t0=time.monotonic();base,key,model=_cfg(g)
 if not base or not model:yield _sse('error',{'reason':'not_configured'});return
 ckey=None
 if cacheable(query,history):
  ckey=tc.make_key('answer',tc.norm_query(query),base,model,os.getenv('DIRECT_TEMPERATURE','0.3'),os.getenv('DIRECT_MAX_TOKENS','700'),os.getenv('DIRECT_EXTRA_BODY',''),hashlib.sha256(SYSTEM.encode()).hexdigest()[:12])
  hit=await tc.get('answer',ckey)
  if hit:yield _sse('meta',{'model':model,'cached':True});yield _sse('token',{'t':hit});yield _sse('done',{'ttft_ms':0,'total_ms':round((time.monotonic()-t0)*1000),'tokens':1,'cached':True});return
 if _active[0]>=MAX_CONCURRENT:yield _sse('error',{'reason':'busy'});return
 _active[0]+=1
 try:
  msgs=[{'role':'system','content':SYSTEM}]
  for h in (history or [])[-6:]:
   if h.get('role') in ('user','assistant') and isinstance(h.get('content'),str):msgs.append({'role':h['role'],'content':h['content'][:2000]})
  msgs.append({'role':'user','content':query[:2000]})
  body={'model':model,'messages':msgs,'temperature':float(os.getenv('DIRECT_TEMPERATURE','0.3')),'max_tokens':int(os.getenv('DIRECT_MAX_TOKENS','700')),'stream':True}
  try:body.update(json.loads(os.getenv('DIRECT_EXTRA_BODY','') or '{}'))
  except (ValueError,TypeError):pass
  hdr={'Authorization':f'Bearer {key}'} if key else {}
  client=_client()
  yield _sse('meta',{'model':model})
  # read=TOTAL_S, not FIRST_TOKEN_S: httpx's read timeout bounds the gap between any two chunks of the WHOLE response, so a
  # tight value here would kill a healthy, slow-but-progressing stream (a reasoning model pausing between tokens) well before
  # TOTAL_S. Time-to-first-byte is bounded separately below by asyncio.wait(FIRST_TOKEN_S+3); total elapsed by the manual
  # monotonic check in the read loop.
  req=client.build_request('POST',base+'/chat/completions',json=body,headers=hdr,timeout=httpx.Timeout(connect=3,read=TOTAL_S,write=3,pool=1))
  task=asyncio.ensure_future(client.send(req,stream=True))
  def _drain_task(t):
   """Let an abandoned client.send(...) finish (its own timeout bounds it) instead of cancelling mid-connect -- see jev.py's
   identical reasoning -- then close whatever it returns so the connection is returned to the pool."""
   async def sink():
    try:
     r=await t;await r.aclose()
    except Exception:pass
   asyncio.ensure_future(sink())
  try:done,pending=await asyncio.wait({task},timeout=FIRST_TOKEN_S+3)
  except asyncio.CancelledError:_drain_task(task);raise  # caller (client disconnect) tore down this generator before we even had a response
  if not done:
   _drain_task(task)
   log.warning('direct llm first byte timed out model=%s',model);yield _sse('error',{'reason':'timeout'});return
  try:resp=task.result()
  except (asyncio.TimeoutError,httpx.TimeoutException):yield _sse('error',{'reason':'timeout'});return
  except httpx.HTTPError as e:log.warning('direct llm connect failed reason=%s',type(e).__name__);yield _sse('error',{'reason':'provider_unavailable'});return
  if resp.status_code>=400:
   await resp.aclose();log.warning('direct llm status=%d',resp.status_code);yield _sse('error',{'reason':'provider_unavailable'});return
  ttft=None;tokens=0;end=t0+TOTAL_S;text=[]
  try:
   async for line in resp.aiter_lines():
    if time.monotonic()>end:yield _sse('error',{'reason':'timeout'});return
    line=line.strip()
    if not line or not line.startswith('data:'):continue
    data=line[5:].strip()
    if data=='[DONE]':break
    try:chunk=json.loads(data)
    except ValueError:continue
    delta=((chunk.get('choices') or [{}])[0].get('delta') or {}).get('content')
    if delta:
     if ttft is None:ttft=round((time.monotonic()-t0)*1000)
     tokens+=1;text.append(delta);yield _sse('token',{'t':delta})
  finally:await resp.aclose()
  if tokens==0:yield _sse('error',{'reason':'provider_unavailable'});return
  if ckey:await tc.put('answer',ckey,''.join(text),tokens=tc.est_tokens(query+SYSTEM)+tokens)
  yield _sse('done',{'ttft_ms':ttft,'total_ms':round((time.monotonic()-t0)*1000),'tokens':tokens})
 except Exception:log.exception('direct answer failed');yield _sse('error',{'reason':'provider_unavailable'})
 finally:_active[0]-=1
