"""Single shared ingestion pipeline (extract -> chunk -> embed -> Qdrant + Postgres), modelled on the Dharma module:
the Celery task, the sample-loader CLI and any future admin flow all call ingest_document, so they cannot diverge."""
import hashlib,secrets,shutil,uuid
from pathlib import Path
from qdrant_client import models
from sqlalchemy import delete
from database import Db,Doc,Chunk
from config_store import read_setting
from jev import JevDecisionProvider
from pipeline import DocumentExtractor,StructureChunker
from providers import LocalEmbedder,QdrantVectors
import extensions

PROFILES={'narrative':420,'table_heavy':260,'code':320,'policy':500}
SUPPORTED={'.pdf','.docx','.txt','.md','.html'}

def _jev(db):
    s=lambda k:(lambda:read_setting(db,k,k))
    return JevDecisionProvider(s('TYPESAFE_API_KEY'),s('JEV_MODEL'),s('JEV_BASE_URL'))

def ingest_document(db,doc_id:str,embedder=None,store=None)->dict:
    """Idempotent: re-running replaces the document's chunks and vectors."""
    doc=db.get(Doc,doc_id)
    if not doc:return {'status':'missing'}
    doc.status='processing';db.commit()
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
        store.remove_doc(doc_id);db.execute(delete(Chunk).where(Chunk.doc_id==doc_id))
        store.upsert([models.PointStruct(id=cid,vector=vec,payload={'doc_id':doc_id,'chunk_id':cid,'page':records[n][0],'title':doc.title}) for n,(cid,vec) in enumerate(zip(ids,embeddings))])
        for cid,(page,text) in zip(ids,records):db.add(Chunk(id=cid,doc_id=doc_id,page=page,text=text,token_estimate=max(1,len(text.split())*4//3)))
        doc.status='ready';db.commit();return {'status':'ready','chunks':len(records),'profile':profile}
    except Exception:
        db.rollback();d=db.get(Doc,doc_id)
        if d:d.status='failed';db.commit()
        raise

def register_file(db,path:Path,upload_dir:Path,owner:str='system',acl:str='*',title:str='')->Doc:
    """Copy a local file into the upload dir and create its Doc row (skips exact duplicates by checksum)."""
    if path.suffix.lower() not in SUPPORTED:raise ValueError(f'Unsupported type: {path.suffix}')
    data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
    existing=db.query(Doc).filter(Doc.checksum==digest,Doc.filename==path.name).first()
    if existing:return existing
    doc_id=secrets.token_hex(16);upload_dir.mkdir(parents=True,exist_ok=True);dest=upload_dir/f'{doc_id}_{path.name}';shutil.copyfile(path,dest)
    doc=Doc(id=doc_id,title=title or path.name,filename=path.name,checksum=digest,owner=owner,acl=acl,snapshot_path=str(dest),status='processing');db.add(doc);db.commit();return doc
