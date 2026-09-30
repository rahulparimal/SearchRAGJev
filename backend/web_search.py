"""Web search provider for generic questions. Only the user's query leaves the system, never document text.
Results are untrusted third-party text; they are BM25-ranked and handed to the LLM as untrusted evidence."""
from bm25 import Bm25Index

class DuckDuckGoSearch:
    def fetch(self,query:str,limit:int)->list[dict]:
        from ddgs import DDGS
        try:rows=DDGS(timeout=8).text(query,max_results=limit)
        except Exception:return []
        return [{'title':r.get('title',''),'url':r.get('href',''),'text':r.get('body','')} for r in rows if r.get('href')]

def web_search(query:str,limit:int=8,provider=None)->list[dict]:
    rows=(provider or DuckDuckGoSearch()).fetch(query,max(limit*2,10))
    if not rows:return []
    index=Bm25Index([f"{r['title']} {r['text']}" for r in rows]);bm=dict(index.search(query,len(rows)))
    order=sorted(range(len(rows)),key=lambda i:(bm.get(i,0.0),-i),reverse=True)  # BM25 first; search-engine order breaks ties
    return [dict(rows[i],bm25=round(bm.get(i,0.0),3)) for i in order[:limit]]
