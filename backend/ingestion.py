"""Single shared ingestion pipeline (extract -> chunk -> embed -> Qdrant + Postgres), modelled on the Dharma module:
the Celery task, the sample-loader CLI and any future admin flow all call ingest_document, so they cannot diverge."""
import hashlib,os,secrets,shutil,uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
from qdrant_client import models
from sqlalchemy import delete,select,func
from database import Db,Doc,Chunk
from config_store import read_setting
from jev import JevDecisionProvider
from pipeline import DocumentExtractor,StructureChunker
from providers import LocalEmbedder,QdrantVectors
import extensions
from cache import cache

PROFILES={'narrative':420,'table_heavy':260,'code':320,'policy':500}
SUPPORTED={'.pdf','.docx','.txt','.md','.html'}

def _jev(db):
    s=lambda k:(lambda:read_setting(db,k,k))
    return JevDecisionProvider(s('TYPESAFE_API_KEY'),s('JEV_MODEL'),s('JEV_BASE_URL'))

def _now():return datetime.now(timezone.utc)

def sweep_stale(db,minutes:int|None=None)->int:
    """Mark documents stuck in 'processing' (lost task, dead worker) as failed so they can be requeued; returns the count."""
    minutes=int(os.getenv('STALE_PROCESSING_MINUTES','30')) if minutes is None else minutes;cutoff=_now()-timedelta(minutes=minutes);n=0
    for d in db.scalars(select(Doc).where(Doc.status=='processing')).all():
        t=d.processing_started_at or d.created_at
        if t.tzinfo is None:t=t.replace(tzinfo=timezone.utc)
        if t<cutoff:d.status='failed';n+=1
    db.commit()
    if n:cache.bump('corpus')
    return n

def _retire_superseded(db,doc,store):
    """After a newer version of a file is searchable, drop older versions of the same filename/owner."""
    for old in db.scalars(select(Doc).where(Doc.filename==doc.filename,Doc.owner==doc.owner,Doc.version<doc.version,Doc.id!=doc.id)).all():
        store.remove_doc(old.id);db.execute(delete(Chunk).where(Chunk.doc_id==old.id));db.delete(old)
        try:Path(old.snapshot_path).unlink(missing_ok=True)
        except OSError:pass
    db.commit();cache.bump('corpus')

def ingest_document(db,doc_id:str,embedder=None,store=None)->dict:
    """Idempotent. A previously ready document stays 'ready' (and searchable) during re-ingest: new vectors are upserted first
    (same content -> same deterministic ids), then chunk rows are swapped in ONE DB transaction, then stale vectors are removed."""
    doc=db.get(Doc,doc_id)
    if not doc:return {'status':'missing'}
    was_ready=doc.status=='ready'
    if not was_ready:doc.status='processing'
    doc.processing_started_at=_now();db.commit()
    try:
        pages=DocumentExtractor().extract(Path(doc.snapshot_path).read_bytes(),doc.filename)
        content='\n'.join(t for _,t in pages)[:5000];profile='narrative'
        if read_setting(db,'JEV_CHUNK_PROFILE_ENABLED','JEV_CHUNK_PROFILE_ENABLED').lower()=='true':
            chosen=_jev(db).decide({'title':doc.title,'sample':content},{'profile':{'type':'choice','instructions':'Classify the extracted document to choose a tested chunking profile.','criteria':{'narrative':'Prose and paragraphs','table_heavy':'Primarily tables or tabular data','code':'Code or technical listings','policy':'Policy or procedural sections','other':'None of these'}}}).get('profile',{}).get('choice','other')
            profile=chosen if chosen in PROFILES else 'narrative'
        extensions.run('post_extract',{'document_id':doc_id,'pages':len(pages),'profile':profile,'characters':len(content)})
        chunker=StructureChunker(max_words=PROFILES[profile],overlap=65);records=[]
        for page,text in pages:records.extend(chunker.split(text,page))
        if not records:raise ValueError('No text extracted. OCR is not configured for scanned documents.')
        ids=[str(uuid.uuid5(uuid.NAMESPACE_URL,f'{doc_id}:{n}:{chunk}')) for n,(_,chunk) in enumerate(records)]
        extensions.run('pre_index',{'document_id':doc_id,'chunks':len(records),'profile':profile})
        embeddings=(embedder or LocalEmbedder()).encode([c for _,c in records]);store=store or QdrantVectors()
        store.upsert([models.PointStruct(id=cid,vector=vec,payload={'doc_id':doc_id,'chunk_id':cid,'page':records[n][0],'title':doc.title}) for n,(cid,vec) in enumerate(zip(ids,embeddings))])
        db.execute(delete(Chunk).where(Chunk.doc_id==doc_id))
        for cid,(page,text) in zip(ids,records):db.add(Chunk(id=cid,doc_id=doc_id,page=page,text=text,token_estimate=max(1,len(text.split())*4//3)))
        doc.status='ready';db.commit();cache.bump('corpus')
        store.remove_doc_except(doc_id,ids);_retire_superseded(db,doc,store)
        return {'status':'ready','chunks':len(records),'profile':profile}
    except Exception:
        db.rollback();d=db.get(Doc,doc_id)
        if d and not was_ready:d.status='failed';db.commit();cache.bump('corpus')
        raise

def register_file(db,path:Path,upload_dir:Path,owner:str='system',acl:str='*',title:str='')->Doc:
    """Copy a local file into the upload dir and create its Doc row. Identical content (same filename+checksum) is a no-op;
    changed content under the same filename becomes version n+1 and retires the older version once the new one is indexed."""
    if path.suffix.lower() not in SUPPORTED:raise ValueError(f'Unsupported type: {path.suffix}')
    data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
    existing=db.query(Doc).filter(Doc.checksum==digest,Doc.filename==path.name,Doc.owner==owner).first()
    if existing:return existing
    version=(db.scalar(select(func.max(Doc.version)).where(Doc.filename==path.name,Doc.owner==owner)) or 0)+1
    doc_id=secrets.token_hex(16);upload_dir.mkdir(parents=True,exist_ok=True);dest=upload_dir/f'{doc_id}_{path.name}';shutil.copyfile(path,dest)
    doc=Doc(id=doc_id,title=title or path.name,filename=path.name,checksum=digest,owner=owner,acl=acl,version=version,snapshot_path=str(dest),status='processing',processing_started_at=_now());db.add(doc);db.commit();return doc
