"""Small TTL cache: Redis when REDIS_URL is reachable, in-process LRU otherwise. Also generation counters used to invalidate derived
caches (corpus/settings). Generations are random tokens, so a flushed Redis can never replay an old value."""
import json,os,secrets,threading,time
from collections import OrderedDict
from execpool import run_io

class Cache:
    def __init__(self,size=512):self.lru=OrderedDict();self.size=size;self.lock=threading.Lock();self.r=None;self.down_until=0.0;self.gens={}
    def _redis(self):
        url=os.getenv('REDIS_URL')
        if not url or time.time()<self.down_until:return None
        if self.r is None:
            try:
                import redis
                self.r=redis.Redis.from_url(url,socket_connect_timeout=0.3,socket_timeout=0.3,decode_responses=True)
            except Exception:self.down_until=time.time()+30;return None
        return self.r
    def _fail(self):self.r=None;self.down_until=time.time()+30
    def get(self,key):
        r=self._redis()
        if r:
            try:v=r.get(key);return None if v is None else json.loads(v)
            except Exception:self._fail()
        with self.lock:
            hit=self.lru.get(key)
            if not hit or hit[0]<time.time():self.lru.pop(key,None);return None
            self.lru.move_to_end(key);return hit[1]
    def set(self,key,value,ttl):
        r=self._redis()
        if r:
            try:r.setex(key,int(ttl),json.dumps(value));return
            except Exception:self._fail()
        with self.lock:
            self.lru[key]=(time.time()+ttl,value);self.lru.move_to_end(key)
            while len(self.lru)>self.size:self.lru.popitem(last=False)
    def get_many(self,keys):
        """One MGET round trip when Redis is up; falls back to per-key reads."""
        r=self._redis()
        if r and keys:
            try:return [None if v is None else json.loads(v) for v in r.mget(keys)]
            except Exception:self._fail()
        return [self.get(k) for k in keys]
    def set_many(self,items,ttl):
        r=self._redis()
        if r and items:
            try:
                with r.pipeline() as p:
                    for k,v in items.items():p.setex(k,int(ttl),json.dumps(v))
                    p.execute()
                return
            except Exception:self._fail()
        for k,v in items.items():self.set(k,v,ttl)
    # async wrappers: the sync Redis client must never run on the event loop (bounded io executor instead)
    async def aget(self,key):return await run_io(self.get,key)
    async def aset(self,key,value,ttl):return await run_io(self.set,key,value,ttl)
    async def aset_many(self,items,ttl):return await run_io(self.set_many,items,ttl)
    async def aget_many(self,keys):return await run_io(self.get_many,keys)
    def generation(self,name):
        """Current token for `name`, or None when Redis is unavailable (callers then fall back to scanning)."""
        r=self._redis()
        if not r:return None
        try:
            k=f'gen:v1:{name}';v=r.get(k)
            if v is None:r.set(k,secrets.token_hex(6),nx=True);v=r.get(k)
            return v
        except Exception:self._fail();return None
    def bump(self,name):
        t=secrets.token_hex(6);self.gens[name]=t
        r=self._redis()
        if r:
            try:r.set(f'gen:v1:{name}',t)
            except Exception:self._fail()
        return t
    def local_generation(self,name):return self.gens.get(name,'0')

cache=Cache()
