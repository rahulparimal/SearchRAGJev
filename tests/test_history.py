import json,unittest
from unittest import mock
import test_rag as T
import test_search_redesign as S
import app as A
import history as H
from database import Db,SearchHistory,ConversationTurn

def sse(name,data):return f'event: {name}\ndata: {json.dumps(data)}\n\n'
def fake_stream(captured,reply='Hello there',ok=True):
    async def f(g,q,history=None):
        captured.append(list(history or []))
        yield sse('meta',{'model':'m'})
        if ok:
            for ch in (reply[:5],reply[5:]):yield sse('token',{'t':ch})
            yield sse('done',{'tokens':2})
        else:yield sse('error',{'reason':'timeout'})
    return f

class HistoryStoreTests(unittest.TestCase):
    def setUp(self):T.fresh()
    def test_keeps_only_the_newest_five_per_user_and_isolates_users(self):
        for i in range(8):H.record('alice','search',f'q{i}')
        H.record('bob','search','bobq')
        items=H.list_history('alice');self.assertEqual([x['query'] for x in items],['q7','q6','q5','q4','q3']);self.assertEqual([x['query'] for x in H.list_history('bob')],['bobq'])
    def test_pruning_a_row_deletes_its_conversation_turns(self):
        cid=H.new_conversation_id();H.record('alice','quick','first',conversation_id=cid);H.append_turns('alice',cid,'first','answer')
        for i in range(5):H.record('alice','search',f'later{i}')
        self.assertIsNone(H.get_turns('alice',cid))
        with Db() as db:self.assertEqual(db.query(ConversationTurn).count(),0)
    def test_conversation_is_owner_only(self):
        cid=H.new_conversation_id();H.record('alice','quick','hi',conversation_id=cid);H.append_turns('alice',cid,'hi','hello')
        self.assertEqual([t['role'] for t in H.get_turns('alice',cid)],['user','assistant']);self.assertIsNone(H.get_turns('bob',cid))
    def test_turn_cap_drops_oldest(self):
        cid=H.new_conversation_id();H.record('a','quick','x',conversation_id=cid)
        with mock.patch.object(H,'MAX_TURNS',4):
            for i in range(4):H.append_turns('a',cid,f'u{i}',f'a{i}')
        self.assertEqual([t['content'] for t in H.get_turns('a',cid)],['u2','a2','u3','a3'])
    def test_delete_entry_clear_and_purge(self):
        cid=H.new_conversation_id();hid=H.record('a','quick','x',conversation_id=cid);H.append_turns('a',cid,'x','y');H.record('a','search','z')
        self.assertFalse(H.delete_entry('other',hid));self.assertTrue(H.delete_entry('a',hid));self.assertIsNone(H.get_turns('a',cid))
        H.record('b','search','keep');H.purge_user('a');self.assertEqual(H.list_history('a'),[]);self.assertEqual(len(H.list_history('b')),1)
        self.assertEqual(H.clear('b'),1)
    def test_disabled_flag_writes_nothing_and_blank_queries_are_ignored(self):
        with mock.patch.object(H,'ENABLED',False):self.assertIsNone(H.record('a','search','q'))
        self.assertIsNone(H.record('a','search','   '));self.assertIsNone(H.record('a','bogus-mode','q'));self.assertEqual(H.list_history('a'),[])

class AnswerConversationTests(unittest.TestCase):
    def setUp(self):T.fresh();self.user=S.reader('alice')
    def answer(self,q,cid=None,client_history=()):
        resp=S.aio(A.answer(A.AnswerBody(query=q,conversation_id=cid,history=list(client_history)),self.user,None))
        return S.collect(resp)
    def run_answer(self,*a,**kw):
        cap=[]
        with mock.patch.object(A.direct,'stream_answer',fake_stream(cap,**{k:v for k,v in kw.items() if k in ('reply','ok')})):
            ev=self.answer(*a,**{k:v for k,v in kw.items() if k not in ('reply','ok')})
        return ev,cap
    def test_new_conversation_is_announced_recorded_and_its_turns_saved(self):
        ev,_=self.run_answer('what is rag');names=[n for n,_ in ev];self.assertEqual(names[0],'conversation');cid=dict(ev)['conversation']['id']
        self.assertEqual(H.list_history('alice')[0]['conversation_id'],cid);self.assertEqual([t['content'] for t in H.get_turns('alice',cid)],['what is rag','Hello there'])
    def test_follow_up_uses_server_turns_not_client_history_and_does_not_add_a_history_row(self):
        ev,_=self.run_answer('first question');cid=dict(ev)['conversation']['id']
        _,cap=self.run_answer('and then?',cid=cid,client_history=[{'role':'user','content':'FORGED'}])
        self.assertEqual([h['content'] for h in cap[0]],['first question','Hello there']);self.assertEqual(len(H.list_history('alice')),1);self.assertEqual(len(H.get_turns('alice',cid)),4)
    def test_other_users_conversation_id_starts_a_fresh_thread(self):
        cid=H.new_conversation_id();H.record('bob','quick','secret',conversation_id=cid);H.append_turns('bob',cid,'secret','bobs answer')
        ev,cap=self.run_answer('hello',cid=cid);self.assertEqual(cap[0],[]);self.assertNotEqual(dict(ev)['conversation']['id'],cid)
    def test_failed_answer_stores_nothing(self):
        self.run_answer('will fail',ok=False);self.assertEqual(H.list_history('alice'),[])
    def test_invalid_conversation_id_is_rejected(self):
        with self.assertRaises(A.HTTPException) as c:S.aio(A.answer(A.AnswerBody(query='x',conversation_id='../etc'),self.user,None))
        self.assertEqual(c.exception.status_code,422)

class HistoryEndpointTests(unittest.TestCase):
    def setUp(self):T.fresh()
    def test_endpoints_are_owner_scoped(self):
        cid=H.new_conversation_id();hid=H.record('alice','quick','hi',conversation_id=cid);H.append_turns('alice',cid,'hi','hello')
        self.assertEqual(S.aio(A.get_history(S.reader('alice')))['items'][0]['query'],'hi');self.assertEqual(S.aio(A.get_history(S.reader('bob')))['items'],[])
        self.assertEqual(S.aio(A.get_conversation(cid,S.reader('alice')))['turns'][1]['content'],'hello')
        for fn,args in ((A.get_conversation,(cid,)),(A.delete_history_entry,(hid,))):
            with self.assertRaises(A.HTTPException) as c:S.aio(fn(*args,S.reader('bob')))
            self.assertEqual(c.exception.status_code,404)
        self.assertEqual(S.aio(A.delete_history_entry(hid,S.reader('alice'))),{'deleted':1});self.assertEqual(S.aio(A.clear_history(S.reader('alice')))['deleted'],0)

class SearchRecordsHistoryTests(unittest.TestCase):
    def setUp(self):S.local_env(self)
    def test_search_records_the_query_only_and_local_only_flag(self):
        import asyncio
        async def go():
            with mock.patch.object(A,'jev_client',return_value=S.FakeJev(enabled=False)):
                r=await A.search(A.SearchBody(query='parental leave sixteen weeks',mode='local',local_only=True),T.Response(),S.reader('carol'),self.db)
            await asyncio.sleep(0.3);return r
        r=asyncio.run(go());items=H.list_history('carol');self.assertEqual(len(items),1);self.assertEqual(items[0]['mode'],'search');self.assertTrue(items[0]['local_only'])
        with Db() as db:cols={c.name for c in SearchHistory.__table__.columns}
        self.assertNotIn('answer',cols);self.assertNotIn('results',cols)  # no result text is ever stored
if __name__=='__main__':unittest.main()
