"""Background ingestion worker; API remains responsive for large documents."""
from celery import Celery
from database import Db
from ingestion import ingest_document
import os

celery=Celery('ragsearchjev',broker=os.getenv('REDIS_URL','redis://localhost:6379/0'))
@celery.task(bind=True,autoretry_for=(ConnectionError,TimeoutError),retry_backoff=True,max_retries=3)
def index_document(self,doc_id:str):
    with Db() as db:return ingest_document(db,doc_id)
