"""Hybrid retrieval fusion and ACL filtered lexical search."""
from sqlalchemy import text

class PostgresLexicalSearch:
    def search(self,db,query:str,username:str,is_admin:bool,limit:int=60):
        stmt=text("""SELECT c.id AS chunk_id, c.doc_id, ts_rank_cd(to_tsvector('english',c.text),websearch_to_tsquery('english',:q)) AS score
          FROM chunks c JOIN documents d ON d.id=c.doc_id
          WHERE d.status='ready' AND (d.acl='*' OR :user=ANY(string_to_array(d.acl,',')) OR :admin)
          AND to_tsvector('english',c.text) @@ websearch_to_tsquery('english',:q)
          ORDER BY score DESC LIMIT :lim""")
        return db.execute(stmt,{'q':query,'user':username,'admin':is_admin,'lim':limit}).mappings().all()

def parse_weights(raw:str|None)->dict:
    """RRF_WEIGHTS like 'bm25=1,dense=1,fts=0.6'; a weight of 0 removes that leg."""
    w={'bm25':1.0,'dense':1.0,'fts':0.6}
    for part in (raw or '').split(','):
        k,_,v=part.partition('=')
        try:
            if k.strip() in w:w[k.strip()]=max(0.0,float(v))
        except ValueError:pass
    return w

def reciprocal_rank_fusion(dense_hits,lexical_rows,bm25_rows=(),k=60,weights=None):
    """Weighted RRF: score = sum(w_leg / (k + rank))."""
    w=weights or parse_weights(None);scores={}; payload={}
    if w['dense']>0:
        for rank,hit in enumerate(dense_hits,1):
            cid=(hit.payload or {}).get('chunk_id')
            if cid:scores[cid]=scores.get(cid,0)+w['dense']/(k+rank);payload[cid]=hit.payload
    for leg,rows in (('fts',lexical_rows),('bm25',bm25_rows)):
        if w[leg]<=0:continue
        for rank,row in enumerate(rows,1):
            cid=row['chunk_id'];scores[cid]=scores.get(cid,0)+w[leg]/(k+rank)
            payload.setdefault(cid,{'chunk_id':cid,'doc_id':row['doc_id'],'page':1})
    return sorted(scores.items(),key=lambda x:x[1],reverse=True),payload
