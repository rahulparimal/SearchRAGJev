"""Per-request stage timings (ms). Never records query text."""
import time
from contextlib import contextmanager

class Timer:
    def __init__(self):self.t0=time.perf_counter();self.ms={};self.degraded=[]
    @contextmanager
    def stage(self,name):
        s=time.perf_counter()
        try:yield
        finally:self.add(name,(time.perf_counter()-s)*1000)
    def add(self,name,ms):self.ms[name]=round(self.ms.get(name,0)+ms,1)
    def degrade(self,reason):
        if reason not in self.degraded:self.degraded.append(reason)
    def report(self):
        out={k:self.ms.get(k) for k in ('plan','bm25','fts','dense','web_fetch','web_cache','rerank')};out['total']=round((time.perf_counter()-self.t0)*1000,1);return out
    def header(self):return ', '.join(f'{k};dur={v}' for k,v in self.report().items() if v is not None)
