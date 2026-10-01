"""One merged Jev "plan" call per query: query type, overview-worthiness, route and freshness (typed Choice/Noul only)."""
import re
QTYPES={'navigational':'find a specific site, page or document by name','known_item':'find one specific known item','fact':'answer one factual question','how_to':'steps or instructions','comparison':'compare several things','exploratory':'broad discovery','other':'none of these'}
WH=re.compile(r'^(what|why|how|when|where|who|which|can|could|does|do|did|is|are|was|were|should|will)\b',re.I)
NOSPACE=re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff]')  # Han, kana, Thai, Lao, Myanmar, Khmer: no spaces between words
def word_count(q):
    """Whitespace-delimited tokens containing a letter or digit (hyphen/apostrophe words count once). Scripts written without spaces use a
    character heuristic (about 2 characters per word), so 8 such characters count as 4 words (eligible), 7 do not."""
    words=sum(1 for t in (q or '').split() if re.search(r'\w',t));cjk=len(NOSPACE.findall(q or ''))
    return max(words,cjk//2) if cjk else words
def heuristic_worthy(q):return bool(WH.match(q.strip()) or q.strip().endswith('?'))
def plan_questions(allow_route):
    qs={'query_type':{'type':'choice','instructions':'Classify the search query.','criteria':QTYPES},
        'overview_worthy':{'type':'noul','instructions':'Is the query phrased as a natural-language question or a request for an explanation (not a bare list of keywords, a title or a name) that a short written summary of search results would serve well?','criteria':{'true':'A concise written summary would serve this query','false':'A plain list of results serves better (keyword strings, titles, names, navigation)'}},
        'time_sensitive':{'type':'noul','instructions':'Does the query need recent or current information (news, prices, versions, latest releases)?','criteria':{'true':'Freshness matters','false':'Timeless'}}}
    if allow_route:qs['route']={'type':'choice','instructions':'Decide where to look. "local" if the query asks about internal, uploaded, company or specific-document content. "web" if it is a generic world-knowledge, current-events or how-to question a private collection would not cover. "both" if either could answer it.','criteria':{'local':'About the private document collection','web':'Generic or public knowledge question','both':'Could be answered by either'}}
    return qs
def _f(a,k):
    try:return max(0.0,min(1.0,float(a[k]['noul'])))
    except (KeyError,TypeError,ValueError):return None
def plan_from_answers(a):
    """Parse Jev answers (from a dedicated plan call, or the plan questions piggybacked on the first rerank request) into a plan dict."""
    qt=(a.get('query_type') or {}).get('choice');rt=(a.get('route') or {}).get('choice')
    return {'query_type':qt if qt in QTYPES else 'other','overview_worthy':_f(a,'overview_worthy'),'time_sensitive':_f(a,'time_sensitive'),'route':rt if rt in {'local','web','both'} else None,'source':'jev' if a else 'heuristic'}
async def make_plan(jev,q,allow_route=False):
    """Fallbacks when Jev is absent, slow or partial: intent 'other', heuristic overview-worthiness, route None (BM25 coverage decides)."""
    return plan_from_answers(await jev.decide(q[:500],plan_questions(allow_route)) if jev.enabled else {})
