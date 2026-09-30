"""Okapi BM25 (ported from the Dharma module's bm25.js) for local chunks and web snippets."""
import math,re
from collections import Counter

STOP=set('a an the of to in on for and or is are was were be do does did how what when where who why which can could should would i me my we our you your it its this that with about from by at as get many much'.split())
def content_terms(text:str)->set[str]:return {t for t in tokenize(text) if t not in STOP}

def tokenize(text:str)->list[str]:
    return re.sub(r'[^a-z0-9\s]',' ',(text or '').lower()).split()

class Bm25Index:
    def __init__(self,texts:list[str],k1:float=1.5,b:float=0.75):
        self.k1=k1;self.b=b;self.tf=[Counter(tokenize(t)) for t in texts];self.len=[sum(c.values()) for c in self.tf]
        self.n=len(texts);self.avg=sum(self.len)/(self.n or 1) or 1.0;self.df=Counter()
        for c in self.tf:self.df.update(c.keys())
    def scores(self,query:str)->list[float]:
        terms=set(tokenize(query));out=[]
        for c,ln in zip(self.tf,self.len):
            s=0.0
            for t in terms:
                df=self.df.get(t,0);tf=c.get(t,0)
                if not df or not tf:continue
                idf=math.log(1+(self.n-df+0.5)/(df+0.5))
                s+=idf*tf*(self.k1+1)/(tf+self.k1*(1-self.b+self.b*ln/self.avg))
            out.append(s)
        return out
    def search(self,query:str,limit:int)->list[tuple[int,float]]:
        ranked=sorted(((i,s) for i,s in enumerate(self.scores(query)) if s>0),key=lambda x:x[1],reverse=True)
        return ranked[:limit]

class LocalBm25:
    """BM25 over all ready chunks, rebuilt when the chunk/document count changes (the worker runs in another process).
    Callers must filter by ACL on the returned doc ids before anything else consumes the hits."""
    def __init__(self):self.key=None;self.index=None;self.rows=[]
    def _load(self,db):
        from sqlalchemy import select,func
        from database import Chunk,Doc
        key=(db.scalar(select(func.count()).select_from(Chunk)),db.scalar(select(func.count()).select_from(Doc).where(Doc.status=='ready')))
        if key!=self.key:
            rows=db.execute(select(Chunk.id,Chunk.doc_id,Chunk.text).join(Doc,Doc.id==Chunk.doc_id).where(Doc.status=='ready')).all()
            self.rows=[(r[0],r[1]) for r in rows];self.index=Bm25Index([r[2] for r in rows]);self.key=key
    def search(self,db,query:str,permitted_doc_ids:set[str],limit:int=60)->list[dict]:
        self._load(db);hits=[];want=content_terms(query)
        for i,s in self.index.search(query,self.index.n):
            cid,did=self.rows[i]
            if did in permitted_doc_ids:hits.append({'chunk_id':cid,'doc_id':did,'score':s,'coverage':len(want&self.index.tf[i].keys())/len(want) if want else 0.0})
            if len(hits)>=limit:break
        return hits
