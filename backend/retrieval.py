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

def reciprocal_rank_fusion(dense_hits,lexical_rows,k=60):
    scores={}; payload={}
    for rank,hit in enumerate(dense_hits,1):
        cid=(hit.payload or {}).get('chunk_id')
        if cid:scores[cid]=scores.get(cid,0)+1/(k+rank);payload[cid]=hit.payload
    for rank,row in enumerate(lexical_rows,1):
        cid=row['chunk_id'];scores[cid]=scores.get(cid,0)+1/(k+rank)
        payload.setdefault(cid,{'chunk_id':cid,'doc_id':row['doc_id'],'page':1})
    return sorted(scores.items(),key=lambda x:x[1],reverse=True),payload
