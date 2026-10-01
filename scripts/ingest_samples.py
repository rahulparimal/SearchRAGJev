"""Ingest local files through the same pipeline as the admin upload (no Celery needed).
Usage: python scripts/ingest_samples.py [--acl USERS] [paths...]   (default: docs/samples). Run with .env exported.
The ACL defaults to "*" (all readers) only for docs/samples; any other path needs an explicit --acl (comma-separated usernames, or *)."""
import argparse,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'backend'))
from database import Db,ensure_schema
from ingestion import ingest_document,register_file,SUPPORTED
import os

def main(paths,acl):
    ensure_schema()
    upload_dir=Path(os.getenv('UPLOAD_DIR',ROOT/'uploads'))
    files=[f for p in paths for f in ([p] if p.is_file() else sorted(p.rglob('*'))) if f.suffix.lower() in SUPPORTED]
    if not files:print('No supported files found');return 1
    with Db() as db:
        for f in files:
            doc=register_file(db,f,upload_dir,owner=os.getenv('ADMIN_USERNAME','admin'),acl=acl)
            if doc.status=='ready':print(f'skip  {f.name} (already ingested)');continue
            print(f'ingest {f.name}: {ingest_document(db,doc.id)}')
    return 0
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('paths',nargs='*',type=Path);ap.add_argument('--acl');a=ap.parse_args()
    samples=(ROOT/'docs'/'samples').resolve();paths=a.paths or [samples]
    if a.acl is None:
        if not all(p.resolve()==samples or samples in p.resolve().parents for p in paths):ap.error('--acl is required for paths outside docs/samples (use --acl "*" to make them readable by everyone)')
        a.acl='*'
    sys.exit(main(paths,a.acl))
