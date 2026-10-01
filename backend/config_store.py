"""Encrypted application provider configuration. Rows are cached per process and invalidated by the 'settings' generation
(bumped by PUT /admin/config); a 60 s TTL bounds staleness for other processes when Redis is unavailable."""
import os,hashlib,base64,threading,time
from cryptography.fernet import Fernet
from database import Setting,Db
from cache import cache

def key_fernet():return Fernet(base64.urlsafe_b64encode(hashlib.sha256(os.getenv('APP_SECRET_KEY','local-dev-secret-change-me').encode()).digest()))
_snap={'gen':None,'at':0.0,'enc':{},'plain':{}};_lock=threading.Lock();TTL=60
def invalidate():cache.bump('settings');_snap['gen']=None
def _enc(db):
    gen=cache.generation('settings') or cache.local_generation('settings')
    with _lock:
        if _snap['gen']==gen and time.time()-_snap['at']<TTL:return _snap['enc']
    rows={r.key:r.encrypted_value for r in db.query(Setting).all()}
    with _lock:_snap.update(gen=gen,at=time.time(),enc=rows,plain={});return rows
def read_setting(db,key,env):
    enc=_enc(db).get(key)
    if enc is None:return os.getenv(env,'')
    v=_snap['plain'].get(key)
    if v is None:v=key_fernet().decrypt(enc.encode()).decode();_snap['plain'][key]=v
    return v

def snapshot(keys):
    """{key: value} for several settings from ONE cached snapshot, read with its own Session (never the request's Session) so callers can run
    this in a worker thread. A search takes one snapshot instead of a read per setting."""
    with Db() as db:return {k:read_setting(db,k,k) for k in keys}
