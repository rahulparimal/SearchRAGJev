"""Background ingestion worker; API remains responsive for large documents."""
from celery import Celery
from app import Db, Doc, Chunk, Setting, setting, jev
from providers import LocalEmbedder,QdrantVectors
from pipeline import DocumentExtractor, StructureChunker
from qdrant_client import models
from pathlib import Path
import os, uuid

celery=Celery('ragsearchjev',broker=os.getenv('REDIS_URL','redis://localhost:6379/0'))
@celery.task(bind=True,autoretry_for=(ConnectionError,TimeoutError),retry_backoff=True,max_retries=3)
def index_document(self,doc_id:str):
    with Db() as db:
        doc=db.get(Doc,doc_id)
        if not doc:return {'status':'missing'}
        doc.status='processing';db.commit()
        try:
            pages=DocumentExtractor().extract(Path(doc.snapshot_path).read_bytes(),doc.filename)
            content='\n'.join(t for _,t in pages)[:5000]
            profile='narrative'
            if setting(db,'JEV_CHUNK_PROFILE_ENABLED','JEV_CHUNK_PROFILE_ENABLED').lower()=='true':
                chosen=jev(db).decide({'title':doc.title,'sample':content},{'profile':{'type':'choice','instructions':'Classify the extracted document to choose a tested chunking profile.','criteria':{'narrative':'Prose and paragraphs','table_heavy':'Primarily tables or tabular data','code':'Code or technical listings','policy':'Policy or procedural sections','other':'None of these'}}}).get('profile',{}).get('choice','other')
                profile=chosen if chosen in {'narrative','table_heavy','code','policy'} else 'narrative'
            max_words={'narrative':420,'table_heavy':260,'code':320,'policy':500}[profile]
            extension_events=[];_,events=__import__('extensions').run('post_extract',{'document_id':doc_id,'pages':len(pages),'profile':profile,'characters':len(content)});extension_events+=events
            records=[];chunker=StructureChunker(max_words=max_words,overlap=65)
            for page,text in pages:records.extend(chunker.split(text,page))
            if not records:raise ValueError('No text extracted. OCR is not configured for scanned documents.')
            ids=[str(uuid.uuid5(uuid.NAMESPACE_URL,f'{doc_id}:{n}:{chunk}')) for n,(_,chunk) in enumerate(records)]
            __import__('extensions').run('pre_index',{'document_id':doc_id,'chunks':len(records),'profile':profile})
            embeddings=LocalEmbedder().encode([chunk for _,chunk in records]);store=QdrantVectors()
            store.upsert([models.PointStruct(id=cid,vector=vec,payload={'doc_id':doc_id,'chunk_id':cid,'page':records[n][0],'title':doc.title}) for n,(cid,vec) in enumerate(zip(ids,embeddings))])
            for cid,(page,content) in zip(ids,records):db.add(Chunk(id=cid,doc_id=doc_id,page=page,text=content,token_estimate=max(1,len(content.split())*4//3)))
            doc.status='ready';db.commit();return {'status':'ready','chunks':len(records)}
        except Exception:
            db.rollback();doc=db.get(Doc,doc_id);doc.status='failed';db.commit();raise
