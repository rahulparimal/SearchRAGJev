"""Agent router: generic/world-knowledge questions go to web search, questions about the knowledge base go to local search."""
ROUTE_QUESTION={'route':{'type':'choice','instructions':'Decide where to look. "local" if the query asks about internal, uploaded, company or specific-document content. "web" if it is a generic world-knowledge, current-events or how-to question that a private document collection would not cover.','criteria':{'local':'About the private document collection','web':'Generic or public knowledge question'}}}

LOCAL_OVERRIDE=0.6  # best local chunk must contain >=60% of the query's content terms

def resolve_route(choice,local_coverage,web_enabled=True):
    """Returns (route, reason) from a Jev route choice (local/web/both/None) plus the BM25 coverage override. `local_coverage` is the
    share (0..1) of the query's content terms found in the best-matching permitted local chunk."""
    if not web_enabled:return 'local','web_disabled'
    if choice in {'local','web','both'}:
        # A Jev "web" vote is overridden when the local corpus clearly contains the query terms.
        if choice=='web' and local_coverage>=LOCAL_OVERRIDE:return 'local','jev_web_overridden_by_strong_local_match'
        return choice,'jev'
    return ('local','bm25_local_match') if local_coverage>=LOCAL_OVERRIDE else ('web','bm25_no_local_match')

def route_query(query,decide,local_coverage,web_enabled=True):
    """Synchronous variant: asks `decide` (Jev decision callable) itself. Kept for callers without a prefetched plan."""
    if not web_enabled:return 'local','web_disabled'
    return resolve_route(decide(query,ROUTE_QUESTION).get('route',{}).get('choice'),local_coverage,True)
