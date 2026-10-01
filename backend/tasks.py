"""Background ingestion worker; API remains responsive for large documents."""
from celery import Celery
from celery.signals import worker_init
from database import Db,ensure_schema
from ingestion import ingest_document
from providers import LocalEmbedder,QdrantVectors
import os

celery=Celery('ragsearchjev',broker=os.getenv('REDIS_URL','redis://localhost:6379/0'))
_embedder=None;_store=None
def _shared():
    """Lazy per-process singletons: loading the embedding model and Qdrant client per task is slow and wasteful."""
    global _embedder,_store
    if _embedder is None:_embedder=LocalEmbedder()
    if _store is None:_store=QdrantVectors()
    return _embedder,_store
@worker_init.connect
def _init(**_):ensure_schema()
@celery.task(bind=True,autoretry_for=(ConnectionError,TimeoutError),retry_backoff=True,max_retries=3)
def index_document(self,doc_id:str):
    embedder,store=_shared()
    with Db() as db:return ingest_document(db,doc_id,embedder,store)
