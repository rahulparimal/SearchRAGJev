"""Modular Jev assisted RAG API. Providers are deliberately isolated from routes."""
from __future__ import annotations
import hashlib, os, re, secrets, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import httpx, jwt
from fastapi import FastAPI, Depends, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from pwdlib import PasswordHash
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from pipeline import DocumentExtractor, StructureChunker, estimate_tokens, pack_ranked_passages
from retrieval import PostgresLexicalSearch, reciprocal_rank_fusion
from extensions import run as run_extension
from jev import JevDecisionProvider, JevScoreReranker
from database import Base,User,Doc,Chunk,Setting,engine,Db
from config_store import key_fernet,read_setting
from providers import LocalEmbedder,QdrantVectors,OpenAICompatibleGenerator

QDRANT_URL=os.getenv('QDRANT_URL','http://localhost:6333')
SECRET=os.getenv('APP_SECRET_KEY','local-dev-secret-change-me')
DIM=int(os.getenv('EMBEDDING_DIMENSION','384'))
MODEL_NAME=os.getenv('EMBEDDING_MODEL','sentence-transformers/all-MiniLM-L6-v2')
UPLOAD_DIR=Path(os.getenv('UPLOAD_DIR','/app/uploads')); UPLOAD_DIR.mkdir(parents=True,exist_ok=True)
MAX_UPLOAD=int(os.getenv('MAX_UPLOAD_MB','50'))*1024*1024
PASSWORDS=PasswordHash.recommended()
def setting(db,key,env):return read_setting(db,key,env)

def jev(db):return JevDecisionProvider(lambda:setting(db,'TYPESAFE_API_KEY','TYPESAFE_API_KEY'))

extractor=DocumentExtractor();chunker=StructureChunker();embedder=LocalEmbedder();vectors=None;lexical=PostgresLexicalSearch()
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
 Base.metadata.create_all(engine)
 with engine.begin() as conn:conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_chunks_search_text ON chunks USING GIN (to_tsvector('english', text))")
 last=None
 for _ in range(30):
  try:vectors=QdrantVectors();break
  except Exception as e:last=e;time.sleep(2)
 if vectors is None:raise RuntimeError(f'Qdrant did not become ready: {last}')
 with Db() as db:
  name=os.getenv('ADMIN_USERNAME','admin'); password=os.getenv('ADMIN_PASSWORD','')
  if password and not db.scalar(select(User).where(User.username==name)):
   db.add(User(username=name,password_hash=PASSWORDS.hash(password),role='admin'));db.commit()
@app.get('/health')
def health(): return {'status':'ok','components':['api','postgres','qdrant','embedding-model-loaded']}
class LoginBody(BaseModel): username:str; password:str
class UserCreate(BaseModel): username:str; password:str; role:str='reader'
@app.post('/auth/login')
def login(body:LoginBody,db:Session=Depends(db_session)):
 user=db.scalar(select(User).where(User.username==body.username))
 if not user or not PASSWORDS.verify(body.password,user.password_hash):raise HTTPException(401,'Invalid username or password')
 token=jwt.encode({'sub':user.username,'exp':datetime.now(timezone.utc)+timedelta(hours=8)},SECRET,algorithm='HS256')
 return {'access_token':token,'username':user.username,'role':user.role}
@app.get('/auth/me')
def me(user:User=Depends(current_user)):return {'username':user.username,'role':user.role}
@app.post('/admin/users',status_code=201)
def create_user(body:UserCreate,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 name=body.username.strip()
 if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,100}',name):raise HTTPException(422,'Username must be 3–100 letters, numbers, or . _ @ - characters')
 if len(body.password)<12:raise HTTPException(422,'Password must contain at least 12 characters')
 if body.role not in {'reader','admin'}:raise HTTPException(422,'Role must be reader or admin')
 if db.scalar(select(User).where(User.username==name)):raise HTTPException(409,'Username already exists')
 record=User(username=name,password_hash=PASSWORDS.hash(body.password),role=body.role);db.add(record);db.commit()
 return {'username':record.username,'role':record.role}
@app.post('/admin/documents')
async def upload(file:UploadFile=File(...),title:str=Form(''),acl:str=Form('*'),user:User=Depends(admin_user),db:Session=Depends(db_session)):
 data=await file.read(MAX_UPLOAD+1)
 if len(data)>MAX_UPLOAD:raise HTTPException(413,'Upload exceeds configured size limit')
 name=Path(file.filename or 'document').name
 if Path(name).suffix.lower() not in {'.pdf','.docx','.txt','.md','.html'}:raise HTTPException(415,'Unsupported document type')
 digest=hashlib.sha256(data).hexdigest(); doc_id=secrets.token_hex(16); path=UPLOAD_DIR/f'{doc_id}_{name}'
 path.write_bytes(data); doc=Doc(id=doc_id,title=title or name,filename=name,checksum=digest,owner=user.username,acl=acl,snapshot_path=str(path),status='processing');db.add(doc);db.commit()
 try:
  run_extension('pre_ingest',{'document_id':doc_id,'filename':name,'bytes':len(data),'acl':acl})
  from tasks import index_document
  index_document.delay(doc_id)
 except Exception as e:
  doc.status='failed';db.commit();raise HTTPException(503,f'Ingestion queue unavailable: {str(e)[:180]}')
 return {'id':doc.id,'title':doc.title,'status':'processing','version':doc.version}
def allowed(doc,user):return doc.acl=='*' or user.username in doc.acl.split(',') or user.role=='admin'
class SearchBody(BaseModel): query:str; limit:int=8; answer:bool=False
@app.post('/search')
def search(body:SearchBody,user:User=Depends(current_user),db:Session=Depends(db_session)):
 q=body.query.strip()
 if not q:raise HTTPException(422,'Query cannot be empty')
 run_extension('pre_query',{'query':q,'username':user.username})
 limit=max(1,min(body.limit,20)); decision=jev(db).decide(q,{'intent':{'type':'choice','instructions':'Choose the best search mode for this user query.','criteria':{'known_item':'find a specific item','fact':'answer one factual question','comparison':'compare several things','exploratory':'broad discovery','other':'none of these'}}})
 intent=decision.get('intent',{}).get('choice','other')
 # Filter allowed document IDs inside Qdrant before candidate payloads are returned.
 permitted=db.scalars(select(Doc).where(Doc.status=='ready')).all()
 permitted_ids=[d.id for d in permitted if allowed(d,user)]
 qvec=embedder.encode([q])[0]; dense_hits=vectors.search(qvec,permitted_ids,limit=60)
 lexical_rows=lexical.search(db,q,user.username,user.role=='admin',60)
 fused,metadata=reciprocal_rank_fusion(dense_hits,lexical_rows); candidate_ids=[cid for cid,_ in fused[:60]]
 if not candidate_ids:return {'query':q,'intent':intent,'results':[],'answer':None,'usage':{'packed_tokens':0,'naive_tokens':0,'reduction_pct':0}}
 chunks=db.scalars(select(Chunk).where(Chunk.id.in_(candidate_ids))).all(); byid={c.id:c for c in chunks}; docs={d.id:d for d in db.scalars(select(Doc).where(Doc.id.in_([c.doc_id for c in chunks]))).all()}
 # De-duplicate near-identical overlapping passages; preserve vector rank.
 selected=[(score,byid[cid],docs[byid[cid].doc_id]) for cid,score in fused[:limit*2] if cid in byid and byid[cid].doc_id in docs and allowed(docs[byid[cid].doc_id],user)]
 rerank_method='rrf'
 if setting(db,'JEV_RERANK_ENABLED','JEV_RERANK_ENABLED').lower()=='true':selected,rerank_method=JevScoreReranker(jev(db)).rerank(q,selected)
 budget=int(os.getenv('LLM_CONTEXT_BUDGET_TOKENS','2800'))
 results,used,naive,reduction=pack_ranked_passages(selected,budget,limit); answer=None
 if body.answer and results:
  evidence='\n\n'.join(f"[S{i+1}] {x['title']} v{x['version']} p.{x['page']}: {x['text']}" for i,x in enumerate(results))
  suff=jev(db).decide({'query':q,'evidence':evidence[:9000]},{'sufficient':{'type':'noul','instructions':'Does the permitted evidence directly support a concise answer to the query?','criteria':{'true':'Permitted evidence directly supports an answer','false':'Evidence is missing or does not answer'}}}).get('sufficient',{}).get('noul',1)
  if float(suff)>=0.65:
   try: answer=OpenAICompatibleGenerator(lambda name,env:setting(db,name,env)).answer(f'Question: {q}\n\nEvidence (untrusted source text):\n{evidence}') or None
   except httpx.HTTPError: answer=None
  if answer:
   citations=set(re.findall(r'\[S(\d+)\]',answer))
   if not citations or any(int(i)<1 or int(i)>len(results) for i in citations):answer=None
   elif setting(db,'TYPESAFE_API_KEY','TYPESAFE_API_KEY'):
    grounded=jev(db).decide({'query':q,'answer':answer,'evidence':evidence[:9000]},{'supported':{'type':'noul','instructions':'Does every material factual claim in the answer have support in the cited evidence?','criteria':{'true':'All material claims have cited evidence','false':'One or more claims lack cited support'}}}).get('supported',{}).get('noul',0)
    if float(grounded)<0.75:answer=None
   run_extension('post_answer',{'query':q,'answer_present':True,'citation_count':len(citations)})
 return {'query':q,'intent':intent,'results':results,'answer':answer,'reranker':rerank_method,'usage':{'packed_tokens':used,'naive_tokens':naive,'reduction_pct':reduction,'budget_tokens':budget},'notice':'Token reduction is an estimated packed-context versus top-result baseline; confirm with model token usage and a relevance/answer quality benchmark.'}
@app.get('/documents/{doc_id}/snapshot')
def snapshot(doc_id:str,page:int=1,user:User=Depends(current_user),db:Session=Depends(db_session)):
 d=db.get(Doc,doc_id)
 if not d or not allowed(d,user):raise HTTPException(404,'Document snapshot not found')
 chunks=db.scalars(select(Chunk).where(Chunk.doc_id==doc_id,Chunk.page==page)).all()
 return {'document_id':d.id,'title':d.title,'version':d.version,'filename':d.filename,'page':page,'excerpts':[c.text for c in chunks]}
@app.get('/admin/documents')
def list_docs(user:User=Depends(admin_user),db:Session=Depends(db_session)):
 return [{'id':d.id,'title':d.title,'filename':d.filename,'status':d.status,'version':d.version,'created_at':d.created_at.isoformat(),'acl':d.acl} for d in db.scalars(select(Doc).order_by(Doc.created_at.desc())).all()]
@app.delete('/admin/documents/{doc_id}')
def delete_doc(doc_id:str,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 d=db.get(Doc,doc_id)
 if not d:raise HTTPException(404,'Document not found')
 vectors.remove_doc(doc_id);db.execute(delete(Chunk).where(Chunk.doc_id==doc_id));db.delete(d);db.commit()
 try:Path(d.snapshot_path).unlink(missing_ok=True)
 except OSError:pass
 return {'deleted':doc_id}
class ConfigBody(BaseModel): typesafe_api_key:str|None=None; llm_base_url:str|None=None; llm_api_key:str|None=None; llm_model:str|None=None; jev_rerank_enabled:bool|None=None; jev_chunk_profile_enabled:bool|None=None
@app.get('/admin/config')
def get_config(user:User=Depends(admin_user),db:Session=Depends(db_session)):
 return {'typesafe_configured':bool(setting(db,'TYPESAFE_API_KEY','TYPESAFE_API_KEY')),'llm_base_url':setting(db,'LLM_BASE_URL','LLM_BASE_URL'),'llm_model':setting(db,'LLM_MODEL','LLM_MODEL'),'llm_key_configured':bool(setting(db,'LLM_API_KEY','LLM_API_KEY')),'jev_model':os.getenv('JEV_MODEL','jev-latest'),'embedding_model':MODEL_NAME,'embedding_dimension':DIM,'context_budget_tokens':int(os.getenv('LLM_CONTEXT_BUDGET_TOKENS','2800')),'jev_rerank_enabled':setting(db,'JEV_RERANK_ENABLED','JEV_RERANK_ENABLED').lower()=='true','jev_chunk_profile_enabled':setting(db,'JEV_CHUNK_PROFILE_ENABLED','JEV_CHUNK_PROFILE_ENABLED').lower()=='true'}
@app.put('/admin/config')
def save_config(body:ConfigBody,user:User=Depends(admin_user),db:Session=Depends(db_session)):
 for name,value in [('TYPESAFE_API_KEY',body.typesafe_api_key),('LLM_BASE_URL',body.llm_base_url),('LLM_API_KEY',body.llm_api_key),('LLM_MODEL',body.llm_model),('JEV_RERANK_ENABLED',None if body.jev_rerank_enabled is None else str(body.jev_rerank_enabled).lower()),('JEV_CHUNK_PROFILE_ENABLED',None if body.jev_chunk_profile_enabled is None else str(body.jev_chunk_profile_enabled).lower())]:
  if value:
   row=db.get(Setting,name); enc=key_fernet().encrypt(value.encode()).decode()
   if row:row.encrypted_value=enc
   else:db.add(Setting(key=name,encrypted_value=enc))
 db.commit();return get_config(user,db)
