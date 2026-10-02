"""Per-user search history and Quick-answer conversations in Postgres. Owner-only: every function takes the username and filters by it, and
there is no admin read path. Keeps the newest KEEP entries per user (deleting an entry deletes its conversation turns). Stores query text and
mode, and for Quick answer the chat turns; never search results, KB passages or research reports."""
import logging,os,uuid
from datetime import datetime,timezone
from sqlalchemy import select,delete,func
from database import Db,SearchHistory,ConversationTurn
log=logging.getLogger('rag.history')
KEEP=int(os.getenv('HISTORY_KEEP','5'));MAX_TURNS=int(os.getenv('HISTORY_MAX_TURNS','20'));ENABLED=os.getenv('HISTORY_ENABLED','true').strip().lower()!='false'
MODES=('quick','search','research')

def new_conversation_id():return uuid.uuid4().hex

def record(username,mode,query,web=False,local_only=False,conversation_id=None):
    """Insert one history row and prune this user's older rows. Never raises (history must not break a search)."""
    if not ENABLED or mode not in MODES or not (query or '').strip():return None
    try:
        with Db() as db:
            hid=uuid.uuid4().hex
            db.add(SearchHistory(id=hid,username=username,mode=mode,query=query.strip()[:2000],web=int(bool(web)),local_only=int(bool(local_only)),conversation_id=conversation_id,created_at=datetime.now(timezone.utc)))
            db.flush()
            old=db.execute(select(SearchHistory.id,SearchHistory.conversation_id).where(SearchHistory.username==username).order_by(SearchHistory.created_at.desc(),SearchHistory.id.desc()).offset(KEEP)).all()
            if old:
                ids=[o[0] for o in old];cids=[o[1] for o in old if o[1]]
                db.execute(delete(SearchHistory).where(SearchHistory.id.in_(ids)))
                if cids:db.execute(delete(ConversationTurn).where(ConversationTurn.conversation_id.in_(cids),ConversationTurn.username==username))
            db.commit();return hid
    except Exception:
        log.warning('history write failed',exc_info=True);return None

def list_history(username):
    with Db() as db:
        rows=db.scalars(select(SearchHistory).where(SearchHistory.username==username).order_by(SearchHistory.created_at.desc(),SearchHistory.id.desc()).limit(KEEP)).all()
        return [{'id':r.id,'mode':r.mode,'query':r.query,'web':bool(r.web),'local_only':bool(r.local_only),'conversation_id':r.conversation_id,'created_at':r.created_at.isoformat() if r.created_at else None} for r in rows]

def clear(username):
    with Db() as db:
        db.execute(delete(ConversationTurn).where(ConversationTurn.username==username));n=db.execute(delete(SearchHistory).where(SearchHistory.username==username)).rowcount;db.commit();return n

def delete_entry(username,hid):
    with Db() as db:
        row=db.scalars(select(SearchHistory).where(SearchHistory.id==hid,SearchHistory.username==username)).first()
        if not row:return False
        if row.conversation_id:db.execute(delete(ConversationTurn).where(ConversationTurn.conversation_id==row.conversation_id,ConversationTurn.username==username))
        db.delete(row);db.commit();return True

def get_turns(username,conversation_id):
    """-> [{'role','content'}] oldest first, or None when this user has no such conversation (other users' ids read as missing)."""
    with Db() as db:
        owns=db.scalars(select(SearchHistory.id).where(SearchHistory.conversation_id==conversation_id,SearchHistory.username==username)).first() or db.scalars(select(ConversationTurn.id).where(ConversationTurn.conversation_id==conversation_id,ConversationTurn.username==username)).first()
        if not owns:return None
        rows=db.scalars(select(ConversationTurn).where(ConversationTurn.conversation_id==conversation_id,ConversationTurn.username==username).order_by(ConversationTurn.idx)).all()
        return [{'role':r.role,'content':r.content} for r in rows]

def append_turns(username,conversation_id,user_text,assistant_text):
    """Add a user+assistant pair; keep only the newest MAX_TURNS turns of this conversation. Never raises."""
    if not ENABLED:return
    try:
        with Db() as db:
            nxt=(db.scalar(select(func.max(ConversationTurn.idx)).where(ConversationTurn.conversation_id==conversation_id,ConversationTurn.username==username)) or 0)
            db.add(ConversationTurn(conversation_id=conversation_id,username=username,idx=nxt+1,role='user',content=user_text[:4000]))
            db.add(ConversationTurn(conversation_id=conversation_id,username=username,idx=nxt+2,role='assistant',content=assistant_text[:4000]));db.flush()
            idxs=db.scalars(select(ConversationTurn.idx).where(ConversationTurn.conversation_id==conversation_id,ConversationTurn.username==username).order_by(ConversationTurn.idx.desc()).offset(MAX_TURNS)).all()
            if idxs:db.execute(delete(ConversationTurn).where(ConversationTurn.conversation_id==conversation_id,ConversationTurn.username==username,ConversationTurn.idx.in_(idxs)))
            db.commit()
    except Exception:log.warning('history turn write failed',exc_info=True)

def purge_user(username):
    with Db() as db:
        db.execute(delete(ConversationTurn).where(ConversationTurn.username==username));db.execute(delete(SearchHistory).where(SearchHistory.username==username));db.commit()
