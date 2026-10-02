"""Okapi BM25 (ported from the Dharma module's bm25.js) for local chunks and web snippets."""
import hashlib,math,re,threading,unicodedata
from collections import Counter,OrderedDict
from cache import cache

STOP=set('a an the of to in on for and or is are was were be do does did how what when where who why which can could should would i me my we our you your it its this that with about from by at as get many much'.split())
def content_terms(text:str)->set[str]:return {t for t in tokenize(text) if t not in STOP}

def fold_accents(text:str)->str:
    """NFKD-decompose and drop combining marks: 'Viṣṇu'->'Visnu', 'Śiva'->'Siva', 'café'->'cafe'. Matches Postgres's
    unaccent() exactly (verified), so a query typed without diacritics can match transliterated/accented source text that
    otherwise tokenizes to nothing useful (the old `[^a-z0-9\\s]` strip turned 'viṣṇu' into 'vi u')."""
    return ''.join(c for c in unicodedata.normalize('NFKD',text or '') if not unicodedata.combining(c))
def tokenize(text:str)->list[str]:
    return re.sub(r'[^a-z0-9\s]',' ',fold_accents(text).lower()).split()

def _fold_same_length(text:str)->str:
    """Per-character accent fold that always preserves string length 1:1 with the input (unlike fold_accents, which is fine
    to change length for tokenize()): each char's base form replaces it only when that decomposes to exactly one character,
    so offsets computed against the folded string stay valid against the original `text` for highlighting."""
    out=[]
    for ch in text or '':
        base=''.join(c for c in unicodedata.normalize('NFKD',ch) if not unicodedata.combining(c))
        out.append(base if len(base)==1 else ch)
    return ''.join(out)
def highlight_spans(text:str,query:str,cap:int=24)->list[list[int]]:
    """[start,end] offsets of query content terms in `text`, merged; the client renders them as text nodes (never HTML)."""
    terms=sorted(content_terms(query),key=len,reverse=True)
    if not terms or not text:return []
    folded=_fold_same_length(text)  # search on the folded text (same length as `text`) so an unaccented query term can still match accented source text
    spans=sorted([m.start(),m.end()] for m in re.finditer(r'\b(?:'+'|'.join(re.escape(t) for t in terms)+r')\b',folded,re.I));out=[]
    for a,b in spans:
        if out and a<=out[-1][1]:out[-1][1]=max(out[-1][1],b)
        else:out.append([a,b])
    return out[:cap]

class Bm25Index:
    def __init__(self,texts:list[str],k1:float=1.5,b:float=0.75):
        self.k1=k1;self.b=b;self.tf=[Counter(tokenize(t)) for t in texts];self.len=[sum(c.values()) for c in self.tf]
        self.n=len(texts);self.avg=sum(self.len)/(self.n or 1) or 1.0;self.df=Counter()
        for c in self.tf:self.df.update(c.keys())
    def scores(self,query:str)->list[float]:
        terms=content_terms(query) or set(tokenize(query));out=[]  # content terms only: stop words (how/can/i...) must not decide the lexical ranking
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
    """BM25 over ready chunks the caller may see. Corpus rows are cached under a content key (chunk ids are content-derived, so a
    same-count replacement changes the key) and an index is built per permitted-document set (small LRU) so idf/avg-length statistics
    never include documents the user cannot read. Callers must still ACL-filter before anything else consumes the hits."""
    LRU=8
    def __init__(self):self.lock=threading.Lock();self.state=(None,[]);self.indexes=OrderedDict()
    def _key(self,db):
        g=cache.generation('corpus')  # bumped by ingest/delete/requeue/upload: no per-request scan of every chunk id
        if g is not None:return ('gen',g)
        from sqlalchemy import select,text
        from database import Chunk,Doc
        if db.bind.dialect.name=='postgresql':
            n,h=db.execute(text("SELECT count(*),coalesce(md5(string_agg(c.id,',' ORDER BY c.id)),'') FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE d.status='ready'")).one();return (n,h)
        ids=sorted(db.scalars(select(Chunk.id).join(Doc,Doc.id==Chunk.doc_id).where(Doc.status=='ready')).all());return (len(ids),hashlib.blake2b(','.join(ids).encode(),digest_size=16).hexdigest())
    def _rows(self,db):
        from sqlalchemy import select
        from database import Chunk,Doc
        key=self._key(db)
        with self.lock:
            if self.state[0]==key:return key,self.state[1]
        rows=[tuple(r) for r in db.execute(select(Chunk.id,Chunk.doc_id,Chunk.text).join(Doc,Doc.id==Chunk.doc_id).where(Doc.status=='ready')).all()]
        with self.lock:
            if self.state[0]!=key:self.state=(key,rows);self.indexes.clear()  # (key,rows) swapped together; derived indexes dropped
            return key,self.state[1]
    def _index(self,key,rows,permitted):
        pk=(key,hashlib.blake2b(','.join(sorted(permitted)).encode(),digest_size=16).hexdigest())
        with self.lock:
            hit=self.indexes.get(pk)
            if hit:self.indexes.move_to_end(pk);return hit
        sub=[(r[0],r[1],r[2]) for r in rows if r[1] in permitted];built=([(c,d) for c,d,_ in sub],Bm25Index([t for _,_,t in sub]))
        with self.lock:
            if self.state[0]==key:self.indexes[pk]=built;[self.indexes.popitem(last=False) for _ in range(max(0,len(self.indexes)-self.LRU))]
        return built
    def search(self,db,query:str,permitted_doc_ids:set[str],limit:int=60)->list[dict]:
        if not permitted_doc_ids:return []
        key,rows=self._rows(db);ids,index=self._index(key,rows,permitted_doc_ids);want=content_terms(query);hits=[]
        for i,s in index.search(query,limit):
            cid,did=ids[i];hits.append({'chunk_id':cid,'doc_id':did,'score':s,'coverage':len(want&index.tf[i].keys())/len(want) if want else 0.0})
        return hits
