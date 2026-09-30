"""Ingest local files through the same pipeline as the admin upload (no Celery needed).
Usage: python scripts/ingest_samples.py [paths...]   (default: docs/samples). Run with .env exported."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'backend'))
from database import Db,Base,engine
from ingestion import ingest_document,register_file,SUPPORTED
import os

def main(paths):
    Base.metadata.create_all(engine)
    upload_dir=Path(os.getenv('UPLOAD_DIR',ROOT/'uploads'))
    files=[f for p in paths for f in ([p] if p.is_file() else sorted(p.rglob('*'))) if f.suffix.lower() in SUPPORTED]
    if not files:print('No supported files found');return 1
    with Db() as db:
        for f in files:
            doc=register_file(db,f,upload_dir,owner=os.getenv('ADMIN_USERNAME','admin'))
            if doc.status=='ready':print(f'skip  {f.name} (already ingested)');continue
            print(f'ingest {f.name}: {ingest_document(db,doc.id)}')
    return 0
if __name__=='__main__':sys.exit(main([Path(a) for a in sys.argv[1:]] or [ROOT/'docs'/'samples']))
