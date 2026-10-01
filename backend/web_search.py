"""Web search provider for generic questions. Only the user's query leaves the system, never document text.
Results are untrusted third-party text; they are BM25-ranked and handed to the LLM as untrusted evidence."""
import re
from urllib.parse import urlparse
from bm25 import Bm25Index

CITATION=re.compile(r'\[S\d+\]')
def clean_text(text:str,limit:int=600)->str:
    """Strip look-alike citation markers so web text cannot forge [S#] references."""
    return re.sub(r'\s+',' ',CITATION.sub('',text or '')).strip()[:limit]
def safe_url(url:str)->str:
    """Only absolute http(s) URLs are kept; javascript:, data:, file: etc. are dropped."""
    try:u=urlparse((url or '').strip())
    except ValueError:return ''
    return u.geturl() if u.scheme in {'http','https'} and u.netloc else ''

class DuckDuckGoSearch:
    def fetch(self,query:str,limit:int)->list[dict]:
        from ddgs import DDGS
        rows=DDGS(timeout=8).text(query,max_results=limit)  # errors propagate so callers can report a degraded result
        return [{'title':r.get('title',''),'url':r.get('href',''),'text':r.get('body','')} for r in rows]

def web_search(query:str,limit:int=8,provider=None)->list[dict]:
    rows=[dict(title=clean_text(r.get('title'),200),url=u,text=clean_text(r.get('text'))) for r in (provider or DuckDuckGoSearch()).fetch(query,max(limit*2,10)) if (u:=safe_url(r.get('url','')))]
    if not rows:return []
    index=Bm25Index([f"{r['title']} {r['text']}" for r in rows]);bm=dict(index.search(query,len(rows)))
    order=sorted(range(len(rows)),key=lambda i:(bm.get(i,0.0),-i),reverse=True)  # BM25 first; search-engine order breaks ties
    return [dict(rows[i],bm25=round(bm.get(i,0.0),3)) for i in order[:limit]]
