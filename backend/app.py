"""Modular Jev assisted RAG API. Providers are deliberately isolated from routes."""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, secrets, threading, time, uuid
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
import httpx, jwt
from fastapi import FastAPI, Depends, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from pwdlib import PasswordHash
from sqlalchemy import select, delete, func
from urllib.parse import urlparse
from sqlalchemy.orm import Session
from pipeline import DocumentExtractor, StructureChunker, estimate_tokens, pack_ranked_passages
from retrieval import PostgresLexicalSearch, reciprocal_rank_fusion, parse_weights
from bm25 import LocalBm25,highlight_spans,content_terms,tokenize
from agent import resolve_route
from web_search import WebSearcher,build_chain,describe_chain,clean_text
from telemetry import Timer
from execpool import run_io,run_search,run_ov
import logconf,config_store
from cache import cache
import overview as ov,plan as planner,direct,deepresearch as research,tokencache
from extensions import run as run_extension
from urlcheck import validate_provider_url,safe_provider_url
from jev import prewarm, AsyncJev, noul_scores_batch, blend_order, parse_blend
from database import User,Doc,Chunk,Setting,engine,Db,ensure_schema
from ingestion import sweep_stale
from config_store import key_fernet,read_setting,invalidate as settings_changed
from providers import LocalEmbedder,QdrantVectors,OverviewGenerator,LocalCrossEncoder,ChatGenerator,resolve_llm
from security import validate_app_secret

logconf.configure()
QDRANT_URL=os.getenv('QDRANT_URL','http://localhost:6333')
SECRET=os.getenv('APP_SECRET_KEY','local-dev-secret-change-me')
DIM=int(os.getenv('EMBEDDING_DIMENSION','384'))
MODEL_NAME=os.getenv('EMBEDDING_MODEL','sentence-transformers/all-MiniLM-L6-v2')
UPLOAD_DIR=Path(os.getenv('UPLOAD_DIR','/app/uploads')); UPLOAD_DIR.mkdir(parents=True,exist_ok=True)
MAX_UPLOAD=int(os.getenv('MAX_UPLOAD_MB','50'))*1024*1024
PASSWORDS=PasswordHash.recommended()
def setting(src,key,env=None):
 return src.get(key,'') if isinstance(src,dict) else read_setting(src,key,env or key)  # src: a settings snapshot dict (request code) or a Session (scripts, tests)


extractor=DocumentExtractor();chunker=StructureChunker();embedder=LocalEmbedder();cross_encoder=LocalCrossEncoder();vectors=None;lexical=PostgresLexicalSearch();local_bm25=LocalBm25()
app=FastAPI(title='Jev RAG Search API',version='0.1.0')
app.add_middleware(CORSMiddleware,allow_origins=os.getenv('CORS_ORIGINS','http://localhost:5173').split(','),allow_credentials=True,allow_methods=['*'],allow_headers=['*'])
auth=HTTPBearer(auto_error=False)
def db_session():
 db=Db()
 try: yield db
 finally: db.close()
def current_user(creds:Optional[HTTPAuthorizationCredentials]=Depends(auth),db:Session=Depends(db_session)):
 if not creds: raise HTTPException(401,'Sign in required')
 try:
  payload=jwt.decode(creds.credentials,SECRET,algorithms=['HS256']); user=db.scalar(select(User).where(User.username==payload['sub']))
  if not user: raise ValueError()
  return user
 except Exception: raise HTTPException(401,'Invalid or expired session')
def admin_user(user:User=Depends(current_user)):
 if user.role!='admin':raise HTTPException(403,'Administrator role required')
 return user
@app.on_event('startup')
def startup():
 global vectors
 validate_app_secret(SECRET)
 ensure_schema()
 last=None
 for _ in range(30):
  try:vectors=QdrantVectors();break
  except Exception as e:last=e;time.sleep(2)
 if vectors is None:raise RuntimeError(f'Qdrant did not become ready: {last}')
 with Db() as db:
  name=os.getenv('ADMIN_USERNAME','admin'); password=os.getenv('ADMIN_PASSWORD','')
  if password and not db.scalar(select(User).where(User.username==name)):
   db.add(User(username=name,password_hash=PASSWORDS.hash(password),role='admin'));db.commit()
  sweep_stale(db)
 threading.Thread(target=warm_embedder,daemon=True).start()
 if os.getenv('LOCAL_RERANKER_ENABLED','true').strip().lower()!='false':threading.Thread(target=warm_cross_encoder,daemon=True).start()  # env only (no DB/pool yet at this point in startup); skip the download/warm entirely when disabled
@app.on_event('startup')
async def warm_jev():
 try:
  j=jev_client(await run_io(config_store.snapshot,SEARCH_KEYS))
  if j.enabled:await prewarm(j.base)  # keep-alive connections ready before the first search
 except Exception:log.warning('jev prewarm failed',exc_info=True)
def warm_embedder():
 try:embedder.warm()
 except Exception:log.exception('embedder warm-up failed')
def warm_cross_encoder():
 try:cross_encoder.warm()
 except Exception:log.exception('local cross-encoder warm-up failed')  # non-fatal: the fallback reranker just stays unavailable; RRF order is still returned
@app.get('/health')
def health():
 parts={}
 def probe(name,fn):
  try:fn();parts[name]='ok'
  except Exception as e:parts[name]=f'error: {type(e).__name__}'
 def pg():
  with engine.connect() as c:c.exec_driver_sql('SELECT 1')
 def qd():
  from qdrant_client import QdrantClient
  if not QdrantClient(url=QDRANT_URL,timeout=2).collection_exists(vectors.collection if vectors else 'rag_chunks_v1'):raise RuntimeError('collection missing')
 def rd():
  import redis
  redis.Redis.from_url(os.getenv('REDIS_URL','redis://localhost:6379/0'),socket_connect_timeout=2,socket_timeout=2).ping()
 probe('postgres',pg);probe('qdrant',qd);probe('redis',rd)
 ok=all(v=='ok' for v in parts.values());parts['embedder']='ok' if embedder.ready else 'warming'  # warming is informational, not unhealthy
 from jev import health_snapshot
 return JSONResponse({'status':'ok' if ok else 'degraded','components':parts,'jev':health_snapshot(),'research_active':research._active[0],'token_cache':tokencache.snapshot()},status_code=200 if ok else 503)
class LoginBody(BaseModel): username:str; password:str
class UserCreate(BaseModel): username:str; password:str; role:str='reader'
@app.post('/auth/login')
def login(body:LoginBody,db:Session=Depends(db_session)):
 user=db.scalar(select(User).where(User.username==body.username))
 if not user or not PASSWORDS.verify(body.password,user.password_hash):raise HTTPException(401,'Invalid username or password')
 token=jwt.encode({'sub':user.username,'exp':datetime.now(timezone.utc)+timedelta(hours=8)},SECRET,algorithm='HS256')
 return {'access_token':token,'username':user.username,'role':user.role}
def research_status(db):
 """For /auth/me: never leaks keys, just whether Deep Research is usable and why not if not."""
 if local_only_enforced(db):return {'available':False,'reason':'local_only'}
 if not web_enabled(db):return {'available':False,'reason':'web_disabled'}
 if setting(db,'DEEP_RESEARCH_ENABLED','DEEP_RESEARCH_ENABLED').strip().lower()!='true':return {'available':False,'reason':'disabled'}
 model=resolve_llm(lambda a,b:setting(db,a,b),[('DEEP_RESEARCH_LLM_BASE_URL','DEEP_RESEARCH_LLM_API_KEY','DEEP_RESEARCH_LLM_MODEL'),('OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_MODEL'),('LLM_BASE_URL','LLM_API_KEY','LLM_MODEL')])[2]
 if not model:return {'available':False,'reason':'not_configured'}
 if setting(db,'DEEP_RESEARCH_REQUIRE_JEV','DEEP_RESEARCH_REQUIRE_JEV').strip().lower()!='false' and not bool(setting(db,'TYPESAFE_API_KEY','TYPESAFE_API_KEY')):return {'available':False,'reason':'needs_jev'}
 return {'available':True,'reason':None,'model':model}
def setup_status(db):
 """For /auth/me: whether the deployment has any LLM / Jev entry at all (no secrets), so the UI can offer first-run setup."""
 llm=direct.configured(lambda a,b:setting(db,a,b));jev=bool(setting(db,'TYPESAFE_API_KEY','TYPESAFE_API_KEY'))
 return {'llm_configured':llm,'jev_configured':jev,'needs_setup':not (llm and jev)}
@app.get('/auth/me')
def me(user:User=Depends(current_user),db:Session=Depends(db_session)):return {'username':user.username,'role':user.role,'web_search':{'enabled':web_enabled(db),'providers':describe_chain(web_searcher().chain)},'local_only_enforced':local_only_enforced(db),'direct':direct.direct_status(lambda a,b:setting(db,a,b)),'research':research_status(db),'setup':setup_status(db),'default_mode':os.getenv('DEFAULT_MODE','quick')}  # no secrets: lets the UI name where web queries go
class LlmProbe(BaseModel): base_url:str; model:str=''; api_key:str=''
def _probe_target(db,body):
 """Validated base URL and key for a connection test. A blank key reuses the saved base-LLM key only when the URL is the saved one, so a
 typed URL can never receive a key that was saved for a different host."""
 try:base=validate_provider_url(body.base_url)
 except ValueError as e:raise HTTPException(422,str(e))
 key=body.api_key.strip()
 if not key and base==(setting(db,'LLM_BASE_URL','LLM_BASE_URL') or '').strip().rstrip('/'):key=setting(db,'LLM_API_KEY','LLM_API_KEY')
 return base,({'Authorization':f'Bearer {key}'} if key else {})
@app.post('/admin/llm/models')
async def llm_models(body:LlmProbe,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 """Lists model ids from an OpenAI-compatible endpoint (/models), which Ollama also serves at /v1/models. Admin only."""
 base,hdr=_probe_target(db,body)
 try:
  async with httpx.AsyncClient(timeout=8) as c:r=await c.get(base+'/models',headers=hdr)
 except httpx.HTTPError as e:return {'ok':False,'error':type(e).__name__,'models':[]}
 if r.status_code>=400:return {'ok':False,'error':f'http_{r.status_code}','models':[]}
 try:items=r.json().get('data') or []
 except (ValueError,AttributeError):return {'ok':False,'error':'bad_response','models':[]}
 return {'ok':True,'models':sorted({m['id'] for m in items if isinstance(m,dict) and isinstance(m.get('id'),str)})[:200]}
@app.post('/admin/llm/test')
async def llm_test(body:LlmProbe,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 """One tiny chat completion to prove the endpoint, key and model work before saving. Reports a short error class, never the response body."""
 if not body.model.strip():raise HTTPException(422,'Choose a model first')
 base,hdr=_probe_target(db,body);t0=time.monotonic()
 try:
  async with httpx.AsyncClient(timeout=45) as c:
   r=await c.post(base+'/chat/completions',headers=hdr,json={'model':body.model.strip(),'messages':[{'role':'user','content':'Reply with the single word OK.'}],'max_tokens':8,'stream':False})
 except httpx.TimeoutException:return {'ok':False,'error':'timeout'}
 except httpx.HTTPError as e:return {'ok':False,'error':type(e).__name__}
 ms=round((time.monotonic()-t0)*1000)
 if r.status_code>=400:return {'ok':False,'error':f'http_{r.status_code}','latency_ms':ms}
 try:good=bool(r.json().get('choices'))
 except (ValueError,AttributeError):good=False
 return {'ok':good,'latency_ms':ms,**({} if good else {'error':'bad_response'})}
@app.post('/admin/users',status_code=201)
def create_user(body:UserCreate,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 name=body.username.strip()
 if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,100}',name):raise HTTPException(422,'Username must be 3–100 letters, numbers, or . _ @ - characters')
 if len(body.password)<12:raise HTTPException(422,'Password must contain at least 12 characters')
 if body.role not in {'reader','admin'}:raise HTTPException(422,'Role must be reader or admin')
 if db.scalar(select(User).where(User.username==name)):raise HTTPException(409,'Username already exists')
 record=User(username=name,password_hash=PASSWORDS.hash(body.password),role=body.role);db.add(record);db.commit()
 return {'username':record.username,'role':record.role}
@app.get('/admin/users')
def list_users(user:User=Depends(admin_user),db:Session=Depends(db_session)):return [{'username':u.username,'role':u.role} for u in db.scalars(select(User).order_by(User.username)).all()]
@app.delete('/admin/users/{username}')
def delete_user(username:str,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 """Admin only. Cannot delete yourself or the last administrator. Existing sessions of the deleted user stop working (every request re-reads the user)."""
 if username==user.username:raise HTTPException(400,'You cannot delete your own account')
 target=db.scalar(select(User).where(User.username==username))
 if not target:raise HTTPException(404,'User not found')
 if target.role=='admin' and (db.scalar(select(func.count()).select_from(User).where(User.role=='admin')) or 0)<=1:raise HTTPException(400,'Cannot delete the last administrator')
 db.delete(target);db.commit();log.info('user deleted by admin');return {'deleted':username}
@app.post('/admin/documents')
def upload(file:UploadFile=File(...),title:str=Form(''),acl:str=Form('*'),user:User=Depends(admin_user),db:Session=Depends(db_session)):
 data=file.file.read(MAX_UPLOAD+1)
 if len(data)>MAX_UPLOAD:raise HTTPException(413,'Upload exceeds configured size limit')
 name=Path(file.filename or 'document').name
 if Path(name).suffix.lower() not in {'.pdf','.docx','.txt','.md','.html'}:raise HTTPException(415,'Unsupported document type')
 digest=hashlib.sha256(data).hexdigest(); doc_id=secrets.token_hex(16); path=UPLOAD_DIR/f'{doc_id}_{name}'
 path.write_bytes(data); doc=Doc(id=doc_id,title=title or name,filename=name,checksum=digest,owner=user.username,acl=acl,snapshot_path=str(path),status='processing',processing_started_at=datetime.now(timezone.utc));db.add(doc);db.commit()
 try:
  run_extension('pre_ingest',{'document_id':doc_id,'filename':name,'bytes':len(data),'acl':acl})
  from tasks import index_document
  index_document.delay(doc_id)
 except Exception as e:
  doc.status='failed';db.commit();raise HTTPException(503,f'Ingestion queue unavailable: {str(e)[:180]}')
 cache.bump('corpus')
 return {'id':doc.id,'title':doc.title,'status':'processing','version':doc.version}
def allowed(doc,user):return doc.acl=='*' or user.username in doc.acl.split(',') or user.role=='admin'
log=logging.getLogger('rag.search')
class SearchBody(BaseModel):
 query:str; limit:int=8; mode:Literal['agent','local','web']='agent'; web:bool=False; overview:bool=False; answer:bool=False  # answer: deprecated alias of overview; /search never returns prose
 rerank:Optional[bool]=None; embed_cache:Optional[bool]=None  # per-request benchmark overrides, administrators only (so a benchmark never flips global settings)
 low_confidence:bool=False; explain:bool=False  # low_confidence: also return passages below the relevance floor (flagged); explain: administrators only, adds per-candidate floor diagnostics
 local_only:bool=False  # "Local KB only": server-enforced, not trusted from the client alone (also forced by LOCAL_ONLY_ENFORCED below) -> no web, no Jev/plan/rerank, no overview eligibility
P=namedtuple('P','username role')  # what request code reads from the user: plain values, safe to share across threads (an ORM instance is bound to one Session)
SEARCH_KEYS=['TYPESAFE_API_KEY','JEV_MODEL','JEV_BASE_URL','JEV_TIMEOUT_MS','JEV_RERANK_ENABLED','JEV_RERANK_TOP_N','LLM_CONTEXT_BUDGET_TOKENS','WEB_SEARCH_ENABLED','WEB_SPECULATIVE','WEB_RERANK_ENABLED','WEB_RERANK_TOP_N','RRF_WEIGHTS','RRF_LEXICAL','RERANK_BLEND','LOCAL_ONLY_ENFORCED','LOCAL_RERANKER_ENABLED']
def local_only_enforced(src):return setting(src,'LOCAL_ONLY_ENFORCED','LOCAL_ONLY_ENFORCED').strip().lower()=='true'
def local_reranker_enabled(src):return setting(src,'LOCAL_RERANKER_ENABLED','LOCAL_RERANKER_ENABLED').strip().lower()!='false'  # on by default: the only reranker available when Jev is off (local-only mode, no key, or an outage)
def web_enabled(src):return setting(src,'WEB_SEARCH_ENABLED','WEB_SEARCH_ENABLED').strip().lower()=='true'
def rerank_enabled(src):
 raw=setting(src,'JEV_RERANK_ENABLED','JEV_RERANK_ENABLED').strip().lower()
 return raw=='true' if raw in {'true','false'} else bool(setting(src,'TYPESAFE_API_KEY','TYPESAFE_API_KEY'))  # on by default whenever Jev is configured
def jev_client(src):return AsyncJev(setting(src,'TYPESAFE_API_KEY','TYPESAFE_API_KEY'),setting(src,'JEV_MODEL','JEV_MODEL'),setting(src,'JEV_BASE_URL','JEV_BASE_URL'),int(setting(src,'JEV_TIMEOUT_MS','JEV_TIMEOUT_MS') or 900))
_web=[None,None]
_WEB_KEYS=['BRAVE_API_KEY','SEARXNG_URL','WEB_PROVIDERS','WEB_DDGS_BACKENDS']
WEB_PROVIDER_CHOICES={'ddgs','brave_api,ddgs','searxng,ddgs','brave_api,searxng,ddgs'}
def web_searcher():
 """Chain comes from admin settings (Web search tab) with env as fallback; rebuilt when those settings change (breaker state resets then)."""
 snap=config_store.snapshot(_WEB_KEYS);sig=tuple(snap.get(k,'') for k in _WEB_KEYS)
 if _web[0] is None or (_web[1] is not None and _web[1]!=sig):_web[0]=WebSearcher(chain=build_chain(lambda k:snap[k] if k in snap else os.getenv(k,'')));_web[1]=sig
 return _web[0]
_acl={'g':None,'docs':[]}
def permitted_ids(db,user):
 """ACL-permitted ready document ids. The (id, acl) list is cached per corpus generation (bumped by ingest/delete/requeue/upload) so no per-search scan."""
 g=cache.generation('corpus')
 if g is None or _acl['g']!=g:
  docs=[(i,a) for i,a in db.execute(select(Doc.id,Doc.acl).where(Doc.status=='ready')).all()]
  if g is not None:_acl.update(g=g,docs=docs)
 else:docs=_acl['docs']
 return [i for i,a in docs if a=='*' or user.username in a.split(',') or user.role=='admin']
def permitted_for(p):
 with Db() as d:return permitted_ids(d,p)  # own Session: runs in a worker thread
def web_notice(chain,meta,degraded):
 """Says exactly what happened: cache hit (nothing sent), the providers a request was actually sent to, or that nothing was sent (breaker open)."""
 sent=meta.get('sent')
 if meta.get('cache_hit'):base='These results came from a recent cache; no new request was made. '
 elif sent is None or sent:base=f"Only your query text was sent to: {describe_chain(chain if sent is None else [p for p in chain if p.name in sent])}. "
 else:base='No web request was made: the search provider is paused after repeated failures. '
 tail=' The web search provider is unavailable, so no web results were returned.' if degraded else (' The web search returned no results.' if meta.get('empty') else '')
 return 'Web results are untrusted third-party text. '+base.strip()+tail
FRESH=re.compile(r'\b(latest|today|tonight|news|current|currently|now|recent|this (week|month|year)|20[2-9]\d|price|release[ds]?)\b',re.I)
def timed(timer,name,fn,*a):
 """Runs a sync leg in the search executor; a failure degrades the stage (and is logged) instead of failing the request."""
 async def go():
  t=time.perf_counter()
  try:return await run_search(fn,*a)
  except Exception as e:timer.degrade(f'{name}_error');log.warning('search leg %s failed reason=%s',name,type(e).__name__,exc_info=True);return None
  finally:timer.add(name,(time.perf_counter()-t)*1000)
 return go()
def leg_bm25(q,permitted):
 with Db() as d:return local_bm25.search(d,q,set(permitted),60)
def leg_fts(q,p):
 with Db() as d:return [dict(r) for r in lexical.search(d,q,p.username,p.role=='admin',60)]
DENSE_TOP_K=int(os.getenv('DENSE_TOP_K','60'));DENSE_MIN=float(os.getenv('DENSE_MIN_SCORE','0.25'));DENSE_ALONE=float(os.getenv('DENSE_ALONE_MIN','0.33'))  # matches the lexical legs' depth (60): an asymmetric pool size hands extra low-rank RRF credit to whichever leg is deeper
# Cosine floors calibrated on the 54-query benchmark (docs/SEARCH_REDESIGN.md): the weakest correct neighbour scores 0.258 (p5 0.345), unrelated queries top out at 0.29-0.30.
# A neighbour below DENSE_MIN is noise; with NO lexical hit at all the best neighbour must reach DENSE_ALONE, otherwise there is no relevant result (empty list).
def leg_dense(q,permitted,use_cache=True):
 if not embedder.ready:return None  # still loading (~7s cold load at process start): BM25/FTS answer without waiting on it; caller degrades 'dense_warming'
 hits=vectors.search(embedder.encode_query(q,use_cache=use_cache),permitted,limit=DENSE_TOP_K)
 return [h for h in hits if getattr(h,'score',1.0)>=DENSE_MIN]
def load_local(fused,p,keep):
 """Fetch fused candidates and re-check ACL against the documents' current rows before anything else consumes them (own Session)."""
 ids=[cid for cid,_ in fused[:max(40,keep)]]
 if not ids:return []
 with Db() as db:
  chunks=db.scalars(select(Chunk).where(Chunk.id.in_(ids))).all();byid={c.id:c for c in chunks};docs={d.id:d for d in db.scalars(select(Doc).where(Doc.id.in_([c.doc_id for c in chunks]))).all()}
  return [(score,byid[cid],docs[byid[cid].doc_id]) for cid,score in fused if cid in byid and byid[cid].doc_id in docs and allowed(docs[byid[cid].doc_id],p)][:keep]
def _ckey(model,q,ident,text,scope):return 'nr:v1:'+hashlib.sha256(f"{model}|{q}|{ident}|{hashlib.sha1(text.encode()).hexdigest()[:10]}|{scope}".encode()).hexdigest()
NEG_TTL=30
async def rerank_order(jev,q,cands,first,prior,timer,top_n,cfg,keys=None,allow_call=True,extra=None,extra_out=None):
 """-> (order, noul{index:score}, method). Batched Noul judgments (JEV_BATCH_SIZE candidates per Jev request, batches in parallel; sized to JEV_RERANK_TOP_N so a full rerank is one request) blended with first-stage rank
 and prior. Scores are cached per (model, query, candidate id, content hash, ACL scope), so a repeat costs no Jev call; a failed attempt is
 negatively cached for 30 s. Partial answers are accepted (an unjudged candidate keeps its first-stage score); no answers -> first-stage order."""
 t=time.perf_counter();n=min(top_n,len(cands))
 if n<2:return list(range(len(cands))),{},'single'
 noul={}
 if keys:noul={i:float(v) for i,v in enumerate(await cache.aget_many(keys[:n])) if v is not None}
 missing=[i for i in range(n) if i not in noul]
 if missing and allow_call and jev.enabled:
  neg='nrneg:v1:'+hashlib.sha256(f"{jev.model}|{q}|{'|'.join(keys[:n]) if keys else len(cands)}".encode()).hexdigest()
  if not await cache.aget(neg):
   dl=int(setting(cfg,'JEV_TIMEOUT_MS','JEV_TIMEOUT_MS') or 900)
   # Batched Noul only: per-candidate (noul_parallel) and the sync score_batch path were removed from the request path (they collapse
   # under concurrency and the latter runs a blocking httpx.post inside the shared io pool); both remain available to offline scripts.
   got=await noul_scores_batch(jev,q,cands,missing,dl,extra=extra,extra_out=extra_out)
   if got:
    noul.update(got)
    if keys:await cache.aset_many({keys[i]:v for i,v in got.items()},1800)
   else:await cache.aset(neg,1,NEG_TTL)
 timer.add('rerank',(time.perf_counter()-t)*1000)
 if not noul:
  if not allow_call:return list(range(len(cands))),{},'skipped'  # nothing was attempted (provider cache hit): not a failure
  timer.degrade('jev_rerank_unavailable');return list(range(len(cands))),{},'fallback'
 partial=len(noul)<n
 if partial:timer.degrade('jev_rerank_partial')
 return blend_order(first,[noul.get(i) for i in range(n)],prior,parse_blend(setting(cfg,'RERANK_BLEND','RERANK_BLEND'))),noul,('jev_partial' if partial else 'jev_noul')
REL_JEV_MIN=float(os.getenv('REL_JEV_MIN','0.5'));REL_CE_MIN=float(os.getenv('REL_CE_MIN','-2.0'));REL_COV_MIN=float(os.getenv('REL_COV_MIN','0.75'));REL_DENSE_MIN=float(os.getenv('REL_DENSE_MIN','0.45'))  # calibrated on docs/eval/search_floor_cases.jsonl (76 cases, one corpus; re-run scripts/floor_eval.py after corpus changes)
FLOOR_STOP={'how','what','why','when','where','which','who','whom','does','do','did','is','are','was','were','can','could','should','would','will','tell','explain','about','please','get','give'}  # question words must not dilute coverage
def apply_floor(q,selected,rel,method,dense_scores):
    """Relevance floor for local hits. Ranking alone never says a passage is RELEVANT: BM25 returns any chunk sharing one content term and the
    nearest dense neighbour always exists, so out-of-corpus queries used to get confident-looking results. A hit is kept when ANY of:
    the reranker judged it relevant (Jev score >= REL_JEV_MIN, or cross-encoder logit >= REL_CE_MIN), it contains >= REL_COV_MIN of the query's
    content terms, or its dense cosine similarity is >= REL_DENSE_MIN. -> ([keep_bool,...], [diagnostic dicts]). Thresholds are env-tunable;
    calibrate with scripts/evaluate.py --floor before changing them."""
    qt=content_terms(q)-FLOOR_STOP;keep=[];diag=[]
    for _,c,d in selected:
        hit_terms=qt&set(tokenize(c.text));cov=(len(hit_terms)/len(qt)) if qt else 1.0;r=rel.get(c.id);dn=(dense_scores or {}).get(c.id);logit=None;ok=False
        if method=='jev_noul' and r is not None:ok=r>=REL_JEV_MIN
        elif method=='cross_encoder' and r is not None:logit=r*16-12;ok=logit>=REL_CE_MIN
        if not ok:ok=cov>=REL_COV_MIN or len(hit_terms)>=3 or (dn is not None and dn>=REL_DENSE_MIN)
        keep.append(ok);diag.append({'title':d.title,'page':c.page,'coverage':round(cov,2),'matched':len(hit_terms),'terms':len(qt),'rel':None if r is None else round(r,3),'ce_logit':None if logit is None else round(logit,2),'dense':None if dn is None else round(dn,3),'kept':ok})
    return keep,diag
async def local_results(p,q,limit,fused,jev,cfg,timer,rank_on,budget,top_n,label_out=None,cross_want=False,dense_scores=None,low_conf=False,explain=False):
 selected=await run_search(load_local,fused,p,max(limit*2,10,top_n if (rank_on or cross_want) else 0));method='rrf';rel={}
 if rank_on and selected:
  # The sync JevScoreReranker path (one blocking httpx.post inside the shared io pool) was removed from the request path; it can
  # starve overview/quick-answer I/O under load. JevScoreReranker/JevDecisionProvider remain available to scripts/ingestion.
  nq=' '.join(q.lower().split());cands=[{'title':d.title,'text':c.text} for _,c,d in selected];keys=[_ckey(jev.model,nq,c.id,c.text,hashlib.sha1(d.acl.encode()).hexdigest()[:8]) for _,c,d in selected]
  order,noul,method=await rerank_order(jev,q,cands,[x[0] for x in selected],None,timer,top_n,cfg,keys,extra=planner.plan_questions(False) if label_out is not None else None,extra_out=label_out)
  method={'fallback':'rrf_fallback','single':'rrf'}.get(method,method)
  rel={selected[i][1].id:v for i,v in noul.items()};selected=[selected[i] for i in order]
 elif cross_want and selected and not cross_encoder.ready:
  timer.degrade('cross_encoder_warming')  # still loading (first few seconds of process life): RRF order is returned rather than blocking this request on the cold load
 elif cross_want and selected:
  # Jev is off (local-only mode, no key, or an admin override) and a local on-box cross-encoder is configured: this is the
  # only reranking those searches would otherwise get, so fall back to it rather than leaving plain RRF order in place.
  t=time.perf_counter();n=min(top_n,len(selected))
  scores=await run_search(cross_encoder.score,q,[c.text[:2000] for _,c,d in selected[:n]])  # 2000 chars comfortably covers a full ~200-word chunk (~1100-1300 chars); 600 was cutting every chunk roughly in half before the model ever saw it -- the same class of bug as the embedder truncation fixed earlier this session. The model's own tokenizer still truncates internally (~512 tokens) as the real backstop.
  timer.add('rerank',(time.perf_counter()-t)*1000)
  order=sorted(range(n),key=lambda i:scores[i],reverse=True)+list(range(n,len(selected)))
  rel={selected[i][1].id:max(0.0,min(1.0,(scores[i]+12)/16)) for i in range(n)}  # ms-marco cross-encoder logits run roughly -12..4; squashed to a 0..1 display range only, not a calibrated probability
  selected=[selected[i] for i in order];method='cross_encoder'
 keep,diag=apply_floor(q,selected,rel,method,dense_scores);below={c.id for (_,c,_),k in zip(selected,keep) if not k};suppressed=0
 if not low_conf:suppressed=len(below);selected=[it for it,k in zip(selected,keep) if k]  # low_confidence keeps everything but flags what is below the floor
 results,used,naive,_=pack_ranked_passages(selected,budget,limit)
 for r in results:
  r.update(source='local',highlights=highlight_spans(r['text'],q),rel=rel.get(r['chunk_id']))
  if low_conf and r['chunk_id'] in below:r['low_confidence']=True
 usage={'packed_tokens':used,'naive_tokens':naive,'budget_tokens':budget,'suppressed':suppressed}
 if explain:usage['floor']=diag
 return results,method,usage
async def web_results(cfg,q,limit,jev,timer,rank_on,fresh,top_n):
 """BM25 + prior (+ optional Jev rerank) over provider snippets. Never embeds or vector-searches web text. Returns (results, method, meta, degraded).
 Jev web rerank is OFF by default (WEB_RERANK_ENABLED=true opts in): on the recorded fixtures it lowered nDCG. A provider cache hit never calls Jev."""
 hits,meta=await web_searcher().search(q,limit,fresh);degraded=not hits and bool(meta.get('error'))
 timer.add('web_cache' if meta.get('cache_hit') else 'web_fetch',meta.get('fetch_ms',0))
 if meta.get('hedged'):timer.degrade('web_primary_slow')
 if degraded:timer.degrade('web_timeout' if meta.get('error')=='timeout' else ('web_breaker_open' if meta.get('error')=='breaker_open' else 'web_unavailable'))
 method='bm25'
 if rank_on and len(hits)>1 and setting(cfg,'WEB_RERANK_ENABLED','WEB_RERANK_ENABLED').strip().lower()=='true':
  nq=' '.join(q.lower().split());tn=int(setting(cfg,'WEB_RERANK_TOP_N','WEB_RERANK_TOP_N') or 10);keys=[_ckey(jev.model,nq,h['url'],h['text'],'web') for h in hits]  # public candidates only: scope 'web'
  order,noul,method=await rerank_order(jev,q,hits,[h['first'] for h in hits],[h['prior'] for h in hits],timer,min(top_n,tn),cfg,keys,allow_call=not meta.get('cache_hit'))
  method={'fallback':'bm25_fallback','single':'bm25','skipped':'bm25'}.get(method,method)
  for i,v in noul.items():hits[i]['rel']=v
  hits=[hits[i] for i in order]
 results=[{'rank':i+1,'score':h['first'],'document_id':'','title':h['title'],'version':0,'page':0,'chunk_id':f'web-{i}','text':h['text'],'snapshot_url':'','url':h['url'],'source':'web','domain':h['domain'],'published':h['published'] or None,'highlights':highlight_spans(h['text'],q),'rel':h.get('rel')} for i,h in enumerate(hits[:limit])]
 return results,method,meta,degraded
@app.post('/search')
async def search(body:SearchBody,response:Response,user:User=Depends(current_user),db:Session=Depends(db_session)):
 q=body.query.strip()
 if not q:raise HTTPException(422,'Query cannot be empty')
 if len(q)>500:raise HTTPException(422,'Query must be at most 500 characters')
 p=P(user.username,user.role)
 if (body.rerank is not None or body.embed_cache is not None) and p.role!='admin':raise HTTPException(403,'rerank / embed_cache overrides are for administrators')
 timer=Timer();cfg=await run_io(config_store.snapshot,SEARCH_KEYS);await run_io(run_extension,'pre_query',{'query':q,'username':p.username})  # all settings: ONE cached snapshot, read off the event loop
 local_only=body.local_only or local_only_enforced(cfg)
 limit=max(1,min(body.limit,20)); mode=body.mode
 if local_only:
  mode='local';body.web=False;body.overview=False;body.answer=False  # one choke point: no web, no overview eligibility; Jev disabled below makes plan/rerank fall away too
 # Local by default: web is used only when the user switched it on for this request AND the deployment allows it. A local-mode search never touches web.
 elif (body.web or mode=='web') and not web_enabled(cfg):raise HTTPException(403,'Web search is disabled for this deployment (set WEB_SEARCH_ENABLED=true or enable it in the admin console)')
 web_on=(not local_only) and (mode=='web' or (mode=='agent' and body.web))
 jev=AsyncJev('') if local_only else jev_client(cfg)  # local_only disables Jev outright, regardless of configured keys: no plan call, no rerank, nothing piggy-backed
 rank_on=(rerank_enabled(cfg) if body.rerank is None else body.rerank) and jev.enabled; top_n=int(setting(cfg,'JEV_RERANK_TOP_N','JEV_RERANK_TOP_N') or 20)
 cross_want=body.rerank is not False and not jev.enabled and local_reranker_enabled(cfg)  # the only rerank available with Jev off (local-only, no key, or admin override): wanted by default, unlike Jev rerank which needs a key to turn on
 budget=max(100,int(setting(cfg,'LLM_CONTEXT_BUDGET_TOKENS','LLM_CONTEXT_BUDGET_TOKENS') or '2800')); use_ecache=body.embed_cache is not False
 async def plan_job():
  t=time.perf_counter();pl=await planner.make_plan(jev,q,web_on and mode=='agent');timer.add('plan',(time.perf_counter()-t)*1000);return pl
 toggle=body.overview or body.answer;must_plan=toggle or (web_on and mode=='agent')  # plan is awaited only when it drives routing or overview eligibility
 plan_t=asyncio.ensure_future(plan_job()) if jev.enabled and must_plan else None  # otherwise the intent label rides on the first rerank request (no extra Jev call)
 label_out={} if (rank_on and not must_plan and mode!='web') else None
 spec=web_on and (mode=='web' or setting(cfg,'WEB_SPECULATIVE','WEB_SPECULATIVE').strip().lower()!='false')  # speculative fetch only for web/agent mode after opt-in; local mode never
 plan=None
 async def need_plan(strict=True):
  nonlocal plan
  if plan is None:
   plan={'query_type':'other','overview_worthy':None,'time_sensitive':None,'route':None,'source':'heuristic'}
   if plan_t:
    if strict:plan=await plan_t
    elif plan_t.done() and not plan_t.cancelled():plan=plan_t.result()  # label only: never hold results for it
    else:plan_t.cancel()
  return plan
 fresh=bool(FRESH.search(q))
 if web_on and not spec and not fresh:fresh=((await need_plan()).get('time_sensitive') or 0)>=0.5
 web_t=asyncio.ensure_future(web_results(cfg,q,limit,jev,timer,rank_on,fresh,top_n)) if spec else None
 results=[];method='rrf';usage={};route=mode if mode!='agent' else 'local';reason='user_selected';meta={};degraded=False;fused=[];dense=None;dense_t=None;bm=fts=None
 permitted=[]
 if mode!='web':
  permitted=await run_search(permitted_for,p);dense_now=mode=='local' or not web_on
  spec_dense=not dense_now and plan_t is not None  # agent + web with a Jev plan in flight: dense runs concurrently with the plan; its result is dropped if the route turns out to be web
  bm,fts,dense=await asyncio.gather(timed(timer,'bm25',leg_bm25,q,permitted),timed(timer,'fts',leg_fts,q,p),timed(timer,'dense',leg_dense,q,permitted,use_ecache) if dense_now else asyncio.sleep(0,result=None))
  if dense_now and dense is None and not embedder.ready:timer.degrade('dense_warming')
  if spec_dense:dense_t=asyncio.ensure_future(timed(timer,'dense',leg_dense,q,permitted,use_ecache))
  coverage=max((h['coverage'] for h in (bm or [])[:5]),default=0.0)
 if mode=='agent':
  if web_on:
   pl=await need_plan();route,reason=resolve_route(pl.get('route'),coverage,True)
  else:route,reason='local','web_disabled'
 elif mode=='local':route,reason='local',('local_only' if local_only else 'user_selected')
 else:route,reason='web','user_selected'
 if route in ('web','both') and web_t is None:web_t=asyncio.ensure_future(web_results(cfg,q,limit,jev,timer,rank_on,fresh,top_n))
 if route=='local' and web_t:web_t.cancel();web_t=None
 if route=='web' and dense_t:dense_t.cancel();dense_t=None
 local_t=None
 if route in ('local','both'):
  if dense is None and dense_t:dense=await dense_t
  elif dense is None and not (mode=='local' or not web_on):dense=await timed(timer,'dense',leg_dense,q,permitted,use_ecache)
  if not (bm or fts) and dense and max(getattr(h,'score',1.0) for h in dense)<DENSE_ALONE:dense=[]  # no lexical evidence and only weak semantic neighbours: nothing relevant
  fused,_=reciprocal_rank_fusion(dense or [],fts or [],bm or [],weights=parse_weights(setting(cfg,'RRF_WEIGHTS','RRF_WEIGHTS')),lexical=(setting(cfg,'RRF_LEXICAL','RRF_LEXICAL') or 'max').strip().lower())
  dscores={(getattr(h,'payload',None) or {}).get('chunk_id'):getattr(h,'score',None) for h in (dense or [])}
  local_t=asyncio.ensure_future(local_results(p,q,limit,fused,jev,cfg,timer,rank_on,budget,top_n,label_out,cross_want,dscores,body.low_confidence,body.explain and p.role=='admin'))
 if local_t:
  results,method,usage=await local_t
  if not results and usage.get('suppressed') and web_on and route=='local':  # nothing confident locally and the user allowed web: look there instead of showing noise
   route,reason='web','local_no_confident_match';web_t=asyncio.ensure_future(web_results(cfg,q,limit,jev,timer,rank_on,fresh,top_n))
 if web_t:
  wres,wmethod,meta,degraded=await web_t
  if route=='both':k=(limit+1)//2;results=results[:k]+wres[:limit-k]
  else:results,method=wres,wmethod
  for i,r in enumerate(results):r['rank']=i+1
 if label_out and plan is None:plan=planner.plan_from_answers(label_out)
 pl=await need_plan(must_plan)
 elig,why=ov.eligibility(q,toggle,pl,len(results))
 if elig and any(r['source']=='web' for r in results) and not jev.enabled:elig,why=False,'web_needs_jev'
 sid=None
 if toggle and results and elig:
  sid=ov.new_search_id();await run_io(ov.save_evidence,sid,p.username,q,elig,route,[{'title':r['title'],'text':r['text'],'url':r.get('url',''),'document_id':r['document_id'],'chunk_id':r['chunk_id'],'version':r['version'],'page':r['page'],'source':r['source'],'rel':r.get('rel')} for r in results[:ov.MAX_SOURCES]])
 for r in results:r.pop('rel',None)
 if jev.enabled and getattr(jev,'down',False):timer.degrade('jev_unavailable');log.warning('jev unavailable for this request: %s',getattr(jev,'last_error',None))
 chain=web_searcher().chain if web_t is not None or route in ('web','both') else []
 notice=web_notice(chain,meta,degraded) if route in ('web','both') else 'Token counts are estimates (packed context versus the top retrieved passages); no savings claim is made without a relevance and answer-quality benchmark.'
 out={'query':q,'intent':pl['query_type'],'route':route,'route_reason':reason,'results':results,'answer':None,'search_id':sid,'overview':{'eligible':elig,'reason':why},'reranker':method,'degraded':degraded,'degraded_stages':timer.degraded,'usage':dict(usage,jev=jev.stats),'notice':notice,'local_only':local_only}
 if not results and route in ('local','both') and not degraded:out['empty_reason']='no_confident_local_match' if usage.get('suppressed') else 'no_relevant_results'
 out['suppressed']=usage.get('suppressed',0) if route in ('local','both') else 0
 out['timings']=timer.report();response.headers['Server-Timing']=timer.header()
 log.info('search route=%s results=%d reranker=%s timings=%s degraded=%s',route,len(results),method,out['timings'],timer.degraded)
 return out
def rebuild_evidence(p,evidence):
 """Local evidence is stored as ids only. Re-read text/title from the database and re-check the user's CURRENT ACL (and ready status) right before
 the LLM call; web evidence (public snippets) is stored as-is. Own Session: runs in a worker thread."""
 ids=[e['chunk_id'] for e in evidence if e.get('source')!='web' and e.get('chunk_id')]
 with Db() as db:
  chunks={c.id:c for c in db.scalars(select(Chunk).where(Chunk.id.in_(ids))).all()} if ids else {}
  docs={d.id:d for d in db.scalars(select(Doc).where(Doc.id.in_({c.doc_id for c in chunks.values()}),Doc.status=='ready')).all()} if chunks else {}
  out=[]
  for e in evidence:
   if e.get('source')=='web':out.append(e);continue
   c=chunks.get(e.get('chunk_id'));d=docs.get(c.doc_id) if c else None
   if c and d and allowed(d,p):out.append(dict(e,text=c.text,title=d.title,url=''))
  return out
class OverviewBody(BaseModel): search_id:str
OV_KEYS=['OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_MODEL','OVERVIEW_LLM_API_KEY','LLM_BASE_URL','LLM_MODEL','LLM_API_KEY','OVERVIEW_TIMEOUT_S','OVERVIEW_ATTEMPT_S','OVERVIEW_MAX_TOKENS','OVERVIEW_EXTRA_BODY','TYPESAFE_API_KEY','JEV_MODEL','JEV_BASE_URL','JEV_TIMEOUT_MS','LOCAL_ONLY_ENFORCED']
@app.post('/search/overview')
async def search_overview(body:OverviewBody,user:User=Depends(current_user),db:Session=Depends(db_session)):
 """SSE: status -> overview | withheld -> done. Evidence comes only from the server-side record for (search_id, user). Fully async: no worker thread is held while the LLM runs."""
 async def one(s):yield s
 hdr={'Cache-Control':'no-cache','X-Accel-Buffering':'no'};p=P(user.username,user.role)
 if local_only_enforced(await run_ov(config_store.snapshot,['LOCAL_ONLY_ENFORCED'])):raise HTTPException(403,'Overviews are disabled: this deployment is local-KB-only')
 rec=await run_ov(ov.load_evidence,body.search_id,p.username)
 if not rec:return StreamingResponse(one(ov.withheld('expired')),media_type='text/event-stream',headers=hdr)
 rec['evidence']=await run_ov(rebuild_evidence,p,rec['evidence'])
 snap=await run_ov(config_store.snapshot,OV_KEYS);jev=jev_client(snap);gen=OverviewGenerator(lambda n,e:snap.get(n,''))
 scope=hashlib.sha256(f'{p.username}|{p.role}'.encode()).hexdigest()[:16]  # per-user scope: users never share cached overviews
 return StreamingResponse(ov.stream_overview(rec,jev,gen,scope,run_extension),media_type='text/event-stream',headers=hdr)
class AnswerBody(BaseModel):
 query:str; history:list[dict]=[]; local_only:bool=False  # local_only: defense in depth for a non-UI client; the UI itself never calls /answer when Local KB only is on
DIRECT_KEYS=['DIRECT_LLM_BASE_URL','DIRECT_LLM_MODEL','DIRECT_LLM_API_KEY','OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_MODEL','OVERVIEW_LLM_API_KEY','LLM_BASE_URL','LLM_MODEL','LLM_API_KEY','LOCAL_ONLY_ENFORCED']
@app.post('/answer')
async def answer(body:AnswerBody,user:User=Depends(current_user),db:Session=Depends(db_session)):
 """SSE 'Quick answer': a direct LLM chat with no retrieval/Jev/web, so it is immune to local-pipeline slowness but is NOT private
 (query+history go to the configured LLM by default) -- disabled outright when the deployment is local-KB-only."""
 q=body.query.strip()
 if not q:raise HTTPException(422,'Query cannot be empty')
 if len(q)>2000:raise HTTPException(422,'Query must be at most 2,000 characters')
 # Trim rather than reject: direct.stream_answer already caps history to the last 6 turns / 2,000 chars each when it builds the
 # provider request, so a long-running Quick chat should keep working (with older turns dropped), not start 422ing mid-conversation.
 history=body.history[-6:]
 hdr={'Cache-Control':'no-cache','X-Accel-Buffering':'no'}
 snap=await run_ov(config_store.snapshot,DIRECT_KEYS)
 if body.local_only or local_only_enforced(snap):raise HTTPException(403,'Quick answers are disabled: this deployment is local-KB-only')
 g=lambda n,e:snap.get(n,'')
 return StreamingResponse(direct.stream_answer(g,q,history),media_type='text/event-stream',headers=hdr)
def research_generators(g):
 """-> (primary, secondary_or_None, role). Prefix is DEEP_RESEARCH_LLM so the admin tab's DEEP_RESEARCH_LLM_* settings are what is read
 (it was 'DEEP_RESEARCH', which silently ignored them). The secondary has no fallback tiers: it is only used when explicitly configured."""
 gen=ChatGenerator(g,prefix='DEEP_RESEARCH_LLM',extra_body_env='DEEP_RESEARCH_EXTRA_BODY',fallback_tiers=[('OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_MODEL'),('LLM_BASE_URL','LLM_API_KEY','LLM_MODEL')])
 gen2=ChatGenerator(g,prefix='DEEP_RESEARCH_2_LLM',extra_body_env='DEEP_RESEARCH_2_EXTRA_BODY',fallback_tiers=[])
 role=(g('DEEP_RESEARCH_SECONDARY_ROLE','DEEP_RESEARCH_SECONDARY_ROLE') or 'failover').strip()
 return gen,(gen2 if gen2.configured() else None),(role if role in ('failover','planner_and_failover') else 'failover')
class ResearchBody(BaseModel):
 query:str; web_consent:bool=False; local_only:bool=False; include_kb:bool=False; refresh:bool=False
RESEARCH_KEYS=['DEEP_RESEARCH_FETCH_PAGES','DEEP_RESEARCH_2_LLM_BASE_URL','DEEP_RESEARCH_2_LLM_MODEL','DEEP_RESEARCH_2_LLM_API_KEY','DEEP_RESEARCH_SECONDARY_ROLE','DEEP_RESEARCH_EXTRA_BODY','DEEP_RESEARCH_2_EXTRA_BODY','DEEP_RESEARCH_ENABLED','DEEP_RESEARCH_LLM_BASE_URL','DEEP_RESEARCH_LLM_MODEL','DEEP_RESEARCH_LLM_API_KEY','OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_MODEL','OVERVIEW_LLM_API_KEY','LLM_BASE_URL','LLM_MODEL','LLM_API_KEY','LOCAL_ONLY_ENFORCED','WEB_SEARCH_ENABLED','TYPESAFE_API_KEY','JEV_MODEL','JEV_BASE_URL','JEV_TIMEOUT_MS']
RESEARCH_PER_USER_PER_HOUR=int(os.getenv('DEEP_RESEARCH_PER_USER_PER_HOUR','10'))
async def _research_kb_search(p,q):
 """Composed from the existing local search legs (same ACL path as /search): run_search off the event loop, fuse, then
 load_local re-checks ACL against each chunk's CURRENT row before anything reaches the report LLM."""
 permitted=await run_search(permitted_for,p)
 bm,fts,dense=await asyncio.gather(run_search(leg_bm25,q,permitted),run_search(leg_fts,q,p),run_search(leg_dense,q,permitted,True))
 fused,_=reciprocal_rank_fusion(dense or [],fts or [],bm or [],lexical='max')
 selected=await run_search(load_local,fused,p,8)
 dscores={(getattr(h,'payload',None) or {}).get('chunk_id'):getattr(h,'score',None) for h in (dense or [])}
 keep,_d=apply_floor(q,selected,{},'rrf',dscores)  # same floor as /search: do not feed irrelevant passages to the report writer
 return [{'title':d.title,'text':c.text,'page':c.page,'document_id':d.id} for (_,c,d),k in zip(selected,keep) if k][:4]
@app.post('/research')
async def research_endpoint(body:ResearchBody,user:User=Depends(current_user),db:Session=Depends(db_session)):
 """SSE Deep Research: query decomposition, parallel web search per sub-question (+ optional local KB), one report-writing
 LLM call, deterministic citation validation, bounded Jev grounding. NOT private (searches public web sources) and NOT
 fast (45s-3min) -- disabled by deployment default, needs explicit per-request consent, and is off entirely for local-only."""
 q=body.query.strip()
 if not q:raise HTTPException(422,'Query cannot be empty')
 if len(q)>1000:raise HTTPException(422,'Query must be at most 1,000 characters')
 hdr={'Cache-Control':'no-cache','X-Accel-Buffering':'no'}
 snap=await run_ov(config_store.snapshot,RESEARCH_KEYS)
 if body.local_only or local_only_enforced(snap):raise HTTPException(403,'Deep Research is disabled: this deployment is local-KB-only')
 if not web_enabled(snap):raise HTTPException(403,'Deep Research is disabled: web search is off for this deployment')
 if setting(snap,'DEEP_RESEARCH_ENABLED','DEEP_RESEARCH_ENABLED').strip().lower()!='true':raise HTTPException(403,'Deep Research is disabled for this deployment')
 if not body.web_consent:raise HTTPException(403,'Deep Research sends your question to public web sources; resend with web_consent=true to proceed')
 p=P(user.username,user.role)
 count=await cache.aincr(f'research_quota:v1:{p.username}:{int(time.time()//3600)}',3600,pool=run_ov)
 if count>RESEARCH_PER_USER_PER_HOUR:raise HTTPException(429,f'Deep Research is limited to {RESEARCH_PER_USER_PER_HOUR} runs per hour per user')
 g=lambda n,e:snap.get(n,'')
 jev=jev_client(snap)
 gen,gen2,role=research_generators(g)
 if setting(snap,'DEEP_RESEARCH_REQUIRE_JEV','DEEP_RESEARCH_REQUIRE_JEV').strip().lower()!='false' and not jev.enabled:
  raise HTTPException(503,'Deep Research needs a Jev key configured (to verify citations) or an admin override (DEEP_RESEARCH_REQUIRE_JEV=false)')
 kb_search=(lambda qq:_research_kb_search(p,qq)) if body.include_kb else None
 return StreamingResponse(research.stream_research(q,web_searcher(),jev,gen,kb_search=kb_search,include_kb=body.include_kb,refresh=body.refresh,gen2=gen2,secondary_role=role,fetch_pages=(setting(snap,'DEEP_RESEARCH_FETCH_PAGES','DEEP_RESEARCH_FETCH_PAGES').strip().lower()=='true') if setting(snap,'DEEP_RESEARCH_FETCH_PAGES','DEEP_RESEARCH_FETCH_PAGES') else None),media_type='text/event-stream',headers=hdr)
@app.get('/documents/{doc_id}/snapshot')
def snapshot(doc_id:str,page:int=1,user:User=Depends(current_user),db:Session=Depends(db_session)):
 d=db.get(Doc,doc_id)
 if not d or not allowed(d,user):raise HTTPException(404,'Document snapshot not found')
 chunks=db.scalars(select(Chunk).where(Chunk.doc_id==doc_id,Chunk.page==page)).all()
 return {'document_id':d.id,'title':d.title,'version':d.version,'filename':d.filename,'page':page,'excerpts':[c.text for c in chunks]}
@app.get('/admin/documents')
def list_docs(user:User=Depends(admin_user),db:Session=Depends(db_session)):
 return [{'id':d.id,'title':d.title,'filename':d.filename,'status':d.status,'version':d.version,'created_at':d.created_at.isoformat(),'acl':d.acl} for d in db.scalars(select(Doc).order_by(Doc.created_at.desc())).all()]
@app.post('/admin/documents/sweep-stale')
def sweep(user:User=Depends(admin_user),db:Session=Depends(db_session)):return {'marked_failed':sweep_stale(db)}
@app.post('/admin/documents/{doc_id}/requeue')
def requeue(doc_id:str,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 d=db.get(Doc,doc_id)
 if not d:raise HTTPException(404,'Document not found')
 was_ready=d.status=='ready'  # a ready document keeps serving searches while it is re-indexed
 if not was_ready:d.status='processing'
 d.processing_started_at=datetime.now(timezone.utc);db.commit();cache.bump('corpus')
 try:
  from tasks import index_document
  index_document.delay(doc_id)
 except Exception as e:
  if not was_ready:d.status='failed';db.commit()
  raise HTTPException(503,f'Ingestion queue unavailable: {str(e)[:180]}')
 return {'id':d.id,'status':d.status}
@app.delete('/admin/documents/{doc_id}')
def delete_doc(doc_id:str,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 d=db.get(Doc,doc_id)
 if not d:raise HTTPException(404,'Document not found')
 vectors.remove_doc(doc_id);db.execute(delete(Chunk).where(Chunk.doc_id==doc_id));db.delete(d);db.commit();cache.bump('corpus')
 try:Path(d.snapshot_path).unlink(missing_ok=True)
 except OSError:pass
 return {'deleted':doc_id}
class ConfigBody(BaseModel): typesafe_api_key:str|None=None; jev_base_url:str|None=None; jev_model:str|None=None; llm_base_url:str|None=None; llm_api_key:str|None=None; llm_model:str|None=None; llm_context_budget_tokens:int|None=None; jev_rerank_enabled:bool|None=None; jev_chunk_profile_enabled:bool|None=None; web_search_enabled:bool|None=None; overview_llm_base_url:str|None=None; overview_llm_model:str|None=None; overview_llm_api_key:str|None=None; direct_llm_base_url:str|None=None; direct_llm_model:str|None=None; direct_llm_api_key:str|None=None; local_only_enforced:bool|None=None; deep_research_enabled:bool|None=None; deep_research_llm_base_url:str|None=None; deep_research_llm_model:str|None=None; deep_research_llm_api_key:str|None=None; deep_research2_llm_base_url:str|None=None; deep_research2_llm_model:str|None=None; deep_research2_llm_api_key:str|None=None; deep_research_secondary_role:str|None=None; web_providers:str|None=None; brave_api_key:str|None=None; searxng_url:str|None=None; deep_research_fetch_pages:bool|None=None; clear:list[str]=[]
CONFIG_FIELD_ENV={'llm_base_url':'LLM_BASE_URL','llm_model':'LLM_MODEL','llm_api_key':'LLM_API_KEY','jev_base_url':'JEV_BASE_URL','jev_model':'JEV_MODEL','typesafe_api_key':'TYPESAFE_API_KEY',
 'overview_llm_base_url':'OVERVIEW_LLM_BASE_URL','overview_llm_model':'OVERVIEW_LLM_MODEL','overview_llm_api_key':'OVERVIEW_LLM_API_KEY','direct_llm_base_url':'DIRECT_LLM_BASE_URL','direct_llm_model':'DIRECT_LLM_MODEL','direct_llm_api_key':'DIRECT_LLM_API_KEY',
 'deep_research_llm_base_url':'DEEP_RESEARCH_LLM_BASE_URL','deep_research_llm_model':'DEEP_RESEARCH_LLM_MODEL','deep_research_llm_api_key':'DEEP_RESEARCH_LLM_API_KEY',
 'deep_research2_llm_base_url':'DEEP_RESEARCH_2_LLM_BASE_URL','deep_research2_llm_model':'DEEP_RESEARCH_2_LLM_MODEL','deep_research2_llm_api_key':'DEEP_RESEARCH_2_LLM_API_KEY','brave_api_key':'BRAVE_API_KEY','searxng_url':'SEARXNG_URL'}
EFFECTIVE_TIERS={'quick':(('Quick answer','DIRECT_LLM_BASE_URL','DIRECT_LLM_API_KEY','DIRECT_LLM_MODEL'),('AI Overview','OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_MODEL'),('Base LLM','LLM_BASE_URL','LLM_API_KEY','LLM_MODEL')),
 'overview':(('AI Overview','OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_MODEL'),('Base LLM','LLM_BASE_URL','LLM_API_KEY','LLM_MODEL')),
 'research_primary':(('Deep Research','DEEP_RESEARCH_LLM_BASE_URL','DEEP_RESEARCH_LLM_API_KEY','DEEP_RESEARCH_LLM_MODEL'),('AI Overview','OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_MODEL'),('Base LLM','LLM_BASE_URL','LLM_API_KEY','LLM_MODEL')),
 'research_secondary':(('Deep Research secondary','DEEP_RESEARCH_2_LLM_BASE_URL','DEEP_RESEARCH_2_LLM_API_KEY','DEEP_RESEARCH_2_LLM_MODEL'),)}
def effective_llms(db):
 """What each feature will really call, and which saved/env tier supplied it, so an override that shadows the base LLM is visible. No secrets."""
 g=lambda a,b:setting(db,a,b);out={}
 for feat,tiers in EFFECTIVE_TIERS.items():
  pending='';ptier=''
  out[feat]={'base':'','model':'','base_tier':None,'model_tier':None,'key_set':False,'source':None}
  for name,be,ke,me in tiers:
   b=(g(be,be) or '').strip();m=(g(me,me) or '').strip()
   if b:
    out[feat]={'base':safe_provider_url(b) or b,'model':pending or m,'base_tier':name,'model_tier':ptier or name,'key_set':bool(g(ke,ke)),'source':'saved' if config_store.in_db(db,be) else 'env'};break
   if m and not pending:pending=m;ptier=name
 return out
def jev_breaker_state(db):
 from jev import breaker
 b=safe_base=(setting(db,'JEV_BASE_URL','JEV_BASE_URL') or 'https://api.typesafe.ai').rstrip('/')
 return breaker(b).state()
@app.get('/admin/config')
def get_config(user:User=Depends(admin_user),db:Session=Depends(db_session)):
 secret_ready=False
 try:validate_app_secret(SECRET);secret_ready=True
 except RuntimeError:pass
 runtime=[{'name':'APP_SECRET_KEY','required':True,'configured':secret_ready,'scope':'deployment','description':'Unique random secret of at least 32 bytes; required for JWT signing and encrypted provider settings.'},{'name':'DATABASE_URL','required':True,'configured':bool(os.getenv('DATABASE_URL')),'scope':'deployment','description':'PostgreSQL connection string used by the API and worker.'},{'name':'QDRANT_URL','required':True,'configured':bool(os.getenv('QDRANT_URL')),'scope':'deployment','description':'Qdrant endpoint; its collection dimension must match the embedding model.'},{'name':'REDIS_URL','required':True,'configured':bool(os.getenv('REDIS_URL')),'scope':'deployment','description':'Redis broker required for asynchronous document indexing.'},{'name':'EMBEDDING_MODEL / EMBEDDING_DIMENSION','required':True,'configured':True,'scope':'deployment','description':f'{MODEL_NAME} · {DIM}; changing either requires a compatible vector collection and reindex.'},{'name':'ADMIN_USERNAME / ADMIN_PASSWORD','required':True,'configured':bool(os.getenv('ADMIN_PASSWORD')),'scope':'first deployment','description':'Used once to create the initial administrator when the user table is empty.'},{'name':'WEB_SEARCH_ENABLED','required':False,'configured':True,'scope':'deployment or admin setting','description':'Currently '+(f'ON: query text is sent to {describe_chain(web_searcher().chain)} only when a user switches Web search on for a request (breakers: {web_searcher().breaker_states()}).' if web_enabled(db) else 'OFF (private default): no query leaves the system for web search.')},{'name':'CORS_ORIGINS','required':False,'configured':bool(os.getenv('CORS_ORIGINS')),'scope':'deployment','description':'Set to the deployed frontend origin in production.'}]
 return {'typesafe_configured':bool(setting(db,'TYPESAFE_API_KEY','TYPESAFE_API_KEY')),'jev_base_url':setting(db,'JEV_BASE_URL','JEV_BASE_URL') or 'https://api.typesafe.ai','llm_base_url':setting(db,'LLM_BASE_URL','LLM_BASE_URL'),'llm_model':setting(db,'LLM_MODEL','LLM_MODEL'),'llm_key_configured':bool(setting(db,'LLM_API_KEY','LLM_API_KEY')),'jev_model':setting(db,'JEV_MODEL','JEV_MODEL') or 'jev-latest','embedding_model':MODEL_NAME,'embedding_dimension':DIM,'context_budget_tokens':int(setting(db,'LLM_CONTEXT_BUDGET_TOKENS','LLM_CONTEXT_BUDGET_TOKENS') or '2800'),'jev_rerank_enabled':rerank_enabled(db),'overview_llm_base_url':setting(db,'OVERVIEW_LLM_BASE_URL','OVERVIEW_LLM_BASE_URL'),'overview_llm_model':setting(db,'OVERVIEW_LLM_MODEL','OVERVIEW_LLM_MODEL'),'overview_llm_key_configured':bool(setting(db,'OVERVIEW_LLM_API_KEY','OVERVIEW_LLM_API_KEY')),'direct_llm_base_url':setting(db,'DIRECT_LLM_BASE_URL','DIRECT_LLM_BASE_URL'),'direct_llm_model':setting(db,'DIRECT_LLM_MODEL','DIRECT_LLM_MODEL'),'direct_llm_key_configured':bool(setting(db,'DIRECT_LLM_API_KEY','DIRECT_LLM_API_KEY')),'direct_status':direct.direct_status(lambda a,b:setting(db,a,b)),'deep_research_enabled':setting(db,'DEEP_RESEARCH_ENABLED','DEEP_RESEARCH_ENABLED').strip().lower()=='true','deep_research_llm_base_url':setting(db,'DEEP_RESEARCH_LLM_BASE_URL','DEEP_RESEARCH_LLM_BASE_URL'),'deep_research_llm_model':setting(db,'DEEP_RESEARCH_LLM_MODEL','DEEP_RESEARCH_LLM_MODEL'),'deep_research_llm_key_configured':bool(setting(db,'DEEP_RESEARCH_LLM_API_KEY','DEEP_RESEARCH_LLM_API_KEY')),'research_status':research_status(db),'effective':effective_llms(db),'deep_research_fetch_pages':(setting(db,'DEEP_RESEARCH_FETCH_PAGES','DEEP_RESEARCH_FETCH_PAGES') or 'false').strip().lower()=='true','deep_research2_llm_base_url':setting(db,'DEEP_RESEARCH_2_LLM_BASE_URL','DEEP_RESEARCH_2_LLM_BASE_URL'),'deep_research2_llm_model':setting(db,'DEEP_RESEARCH_2_LLM_MODEL','DEEP_RESEARCH_2_LLM_MODEL'),'deep_research2_llm_key_configured':bool(setting(db,'DEEP_RESEARCH_2_LLM_API_KEY','DEEP_RESEARCH_2_LLM_API_KEY')),'deep_research_secondary_role':setting(db,'DEEP_RESEARCH_SECONDARY_ROLE','DEEP_RESEARCH_SECONDARY_ROLE') or 'failover','web_providers_setting':setting(db,'WEB_PROVIDERS','WEB_PROVIDERS') or ('brave_api,ddgs' if setting(db,'BRAVE_API_KEY','BRAVE_API_KEY') else 'ddgs'),'brave_configured':bool(setting(db,'BRAVE_API_KEY','BRAVE_API_KEY')),'searxng_url':setting(db,'SEARXNG_URL','SEARXNG_URL'),'local_only_enforced':local_only_enforced(db),'jev_breaker':jev_breaker_state(db),'web_providers':describe_chain(web_searcher().chain),'web_breakers':web_searcher().breaker_states(),'jev_chunk_profile_enabled':setting(db,'JEV_CHUNK_PROFILE_ENABLED','JEV_CHUNK_PROFILE_ENABLED').lower()=='true','web_search_enabled':web_enabled(db),'runtime':runtime}
@app.put('/admin/config')
def save_config(body:ConfigBody,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 if body.llm_context_budget_tokens is not None and not 100<=body.llm_context_budget_tokens<=32000:raise HTTPException(422,'Context budget must be between 100 and 32,000 tokens')
 if body.web_providers is not None and body.web_providers not in WEB_PROVIDER_CHOICES:raise HTTPException(422,'web_providers: choose one of the listed provider options')
 if body.deep_research_secondary_role is not None and body.deep_research_secondary_role not in ('failover','planner_and_failover'):raise HTTPException(422,'deep_research_secondary_role: invalid choice')
 if body.searxng_url:
  try:validate_provider_url(body.searxng_url)
  except ValueError as e:raise HTTPException(422,f'searxng_url: {e}')
 for f,env in CONFIG_FIELD_ENV.items():  # stray whitespace (a trailing space in a model name broke requests) never reaches storage
  v=getattr(body,f,None)
  if isinstance(v,str):setattr(body,f,v.strip())
 clears=[];warnings=[]
 for f in body.clear:
  if f not in CONFIG_FIELD_ENV:raise HTTPException(422,f'clear: unknown field {f}')
  if getattr(body,f,None):raise HTTPException(422,f'clear: {f} cannot be cleared and set in the same request')
  clears.append(CONFIG_FIELD_ENV[f])
 for env,field,keyenv,keyfield,default in (('JEV_BASE_URL','jev_base_url','TYPESAFE_API_KEY','typesafe_api_key','https://api.typesafe.ai'),('LLM_BASE_URL','llm_base_url','LLM_API_KEY','llm_api_key',''),('OVERVIEW_LLM_BASE_URL','overview_llm_base_url','OVERVIEW_LLM_API_KEY','overview_llm_api_key',''),('DIRECT_LLM_BASE_URL','direct_llm_base_url','DIRECT_LLM_API_KEY','direct_llm_api_key',''),('DEEP_RESEARCH_LLM_BASE_URL','deep_research_llm_base_url','DEEP_RESEARCH_LLM_API_KEY','deep_research_llm_api_key',''),('DEEP_RESEARCH_2_LLM_BASE_URL','deep_research2_llm_base_url','DEEP_RESEARCH_2_LLM_API_KEY','deep_research2_llm_api_key','')):
  v=getattr(body,field)
  if not v:continue
  try:new=validate_provider_url(v)  # http/https only, no credentials, no link-local/metadata hosts, optional PROVIDER_URL_ALLOWLIST
  except ValueError as e:raise HTTPException(422,f'{field}: {e}')
  old_url=setting(db,env,env) or default
  if urlparse(old_url).netloc.lower()!=urlparse(new).netloc.lower() and setting(db,keyenv,keyenv) and not getattr(body,keyfield):
   clears.append(keyenv);warnings.append(f'{field}: the provider host changed, so its saved API key was cleared (a saved key is never sent to a different host). Enter a key if the new host needs one.')
  setattr(body,field,new)  # store the normalised URL, not the raw text
 for name,value in [('TYPESAFE_API_KEY',body.typesafe_api_key),('JEV_BASE_URL',body.jev_base_url),('JEV_MODEL',body.jev_model),('LLM_BASE_URL',body.llm_base_url),('LLM_API_KEY',body.llm_api_key),('LLM_MODEL',body.llm_model),('LLM_CONTEXT_BUDGET_TOKENS',None if body.llm_context_budget_tokens is None else str(body.llm_context_budget_tokens)),('JEV_RERANK_ENABLED',None if body.jev_rerank_enabled is None else str(body.jev_rerank_enabled).lower()),('JEV_CHUNK_PROFILE_ENABLED',None if body.jev_chunk_profile_enabled is None else str(body.jev_chunk_profile_enabled).lower()),('WEB_SEARCH_ENABLED',None if body.web_search_enabled is None else str(body.web_search_enabled).lower()),('OVERVIEW_LLM_BASE_URL',body.overview_llm_base_url),('OVERVIEW_LLM_MODEL',body.overview_llm_model),('OVERVIEW_LLM_API_KEY',body.overview_llm_api_key),('DIRECT_LLM_BASE_URL',body.direct_llm_base_url),('DIRECT_LLM_MODEL',body.direct_llm_model),('DIRECT_LLM_API_KEY',body.direct_llm_api_key),('LOCAL_ONLY_ENFORCED',None if body.local_only_enforced is None else str(body.local_only_enforced).lower()),('DEEP_RESEARCH_ENABLED',None if body.deep_research_enabled is None else str(body.deep_research_enabled).lower()),('DEEP_RESEARCH_LLM_BASE_URL',body.deep_research_llm_base_url),('DEEP_RESEARCH_LLM_MODEL',body.deep_research_llm_model),('DEEP_RESEARCH_LLM_API_KEY',body.deep_research_llm_api_key),('DEEP_RESEARCH_2_LLM_BASE_URL',body.deep_research2_llm_base_url),('DEEP_RESEARCH_2_LLM_MODEL',body.deep_research2_llm_model),('DEEP_RESEARCH_2_LLM_API_KEY',body.deep_research2_llm_api_key),('DEEP_RESEARCH_SECONDARY_ROLE',body.deep_research_secondary_role),('WEB_PROVIDERS',body.web_providers),('BRAVE_API_KEY',body.brave_api_key),('SEARXNG_URL',body.searxng_url),('DEEP_RESEARCH_FETCH_PAGES',None if body.deep_research_fetch_pages is None else str(body.deep_research_fetch_pages).lower())]:
  if value:
   row=db.get(Setting,name); enc=key_fernet().encrypt(value.encode()).decode()
   if row:row.encrypted_value=enc
   else:db.add(Setting(key=name,encrypted_value=enc))
 for env in dict.fromkeys(clears):  # an explicit empty value beats the environment (read_setting returns it when a row exists)
  row=db.get(Setting,env);enc=key_fernet().encrypt(b'').decode()
  if row:row.encrypted_value=enc
  else:db.add(Setting(key=env,encrypted_value=enc))
 db.commit();settings_changed();return {**get_config(user,db),'warnings':warnings}
