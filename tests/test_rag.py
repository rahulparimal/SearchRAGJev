import os,sys,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest import mock
_tmp=tempfile.mkdtemp()
os.environ['DATABASE_URL']=f'sqlite:///{_tmp}/t.db';os.environ['UPLOAD_DIR']=f'{_tmp}/up';os.environ['LOCAL_RERANKER_ENABLED']='false'  # avoids downloading/warming a real cross-encoder model in unit tests (network, and it's never 'ready' here anyway)
os.environ.pop('WEB_SEARCH_ENABLED',None);os.environ.pop('REDIS_URL',None);os.environ.pop('WEB_PROVIDERS',None);os.environ.pop('BRAVE_API_KEY',None)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from fastapi import HTTPException
from pydantic import ValidationError
import asyncio
from starlette.responses import Response
import database,ingestion,web_search as ws
from database import Base,Db,Doc,Chunk,User,engine
from bm25 import LocalBm25
import app as A

def fresh():
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    import config_store;config_store.invalidate()  # the settings snapshot is cached per process; a dropped DB must not leak saved settings into the next test
def add_doc(db,did,chunks,status='ready',acl='*',filename=None,owner='system'):
    db.add(Doc(id=did,title=did,filename=filename or did+'.txt',checksum=did,owner=owner,acl=acl,snapshot_path='',status=status))
    db.flush()
    for i,t in enumerate(chunks):db.add(Chunk(id=f'{did}-{i}',doc_id=did,page=1,text=t,token_estimate=5))
    db.commit()

class Bm25Tests(unittest.TestCase):
    def setUp(self):fresh();self.db=Db()
    def tearDown(self):self.db.close()
    def test_acl_filter_and_coverage(self):
        add_doc(self.db,'a',['parental leave is sixteen weeks paid','unrelated filler text here'])
        add_doc(self.db,'b',['secret merger leave plan'],acl='bob')
        b=LocalBm25();hits=b.search(self.db,'parental leave',{'a'})
        self.assertEqual({h['doc_id'] for h in hits},{'a'});self.assertEqual(hits[0]['coverage'],1.0)
        self.assertEqual(b.search(self.db,'merger',{'a'}),[]);self.assertEqual(b.search(self.db,'merger',set()),[])
    def test_stats_exclude_unpermitted_docs(self):
        add_doc(self.db,'a',['vpn reconnect hours','other words entirely'])
        s1=LocalBm25().search(self.db,'vpn',{'a'})[0]['score']
        add_doc(self.db,'b',['vpn vpn vpn '*5,'vpn again and again','vpn'],acl='bob')
        s2=LocalBm25().search(self.db,'vpn',{'a'})[0]['score']
        self.assertAlmostEqual(s1,s2)
    def test_cache_invalidated_on_same_count_replacement(self):
        add_doc(self.db,'a',['original alpha content here'])
        b=LocalBm25();self.assertTrue(b.search(self.db,'alpha',{'a'}))
        self.db.query(Chunk).delete();self.db.add(Chunk(id='a-new',doc_id='a',page=1,text='replacement beta content here',token_estimate=5));self.db.commit()
        self.assertEqual(b.search(self.db,'alpha',{'a'}),[]);self.assertEqual(b.search(self.db,'beta',{'a'})[0]['chunk_id'],'a-new')
    def test_lru_bounded(self):
        add_doc(self.db,'a',['some text to index'])
        b=LocalBm25()
        for i in range(12):b.search(self.db,'text',{'a',f'x{i}'})
        self.assertLessEqual(len(b.indexes),LocalBm25.LRU)

class WebTests(unittest.TestCase):
    def test_scheme_filter_and_citation_strip(self):
        self.assertEqual(ws.safe_url('javascript:alert(1)'),'');self.assertEqual(ws.safe_url('data:text/html,x'),'');self.assertEqual(ws.safe_url('file:///etc/passwd'),'')
        self.assertEqual(ws.safe_url('https://a.example/x'),'https://a.example/x');self.assertEqual(ws.safe_url('/relative'),'')
        class P:
            def fetch(s,q,n):return [{'title':'T [S1]','url':'javascript:x','text':'bad'},{'title':'Good [S2]','url':'http://ok.example','text':'fine [S3] text'}]
        r=ws.web_search('fine text',5,provider=P());self.assertEqual(len(r),1);self.assertNotIn('[S',r[0]['title']+r[0]['text'])
    def test_provider_error_reported_as_degraded(self):
        class Boom:
            name='boom';engines=['boom']
            def fetch(s,q,n,f=None):raise RuntimeError('down')
        searcher=ws.WebSearcher(chain=[Boom()]);hits,meta=asyncio.run(searcher.search('zzz unique degraded query',5))
        self.assertEqual(hits,[]);self.assertIn('error',meta)

def run_search(body,user,db):return asyncio.run(A.search(body,Response(),user,db))
class ModeTests(unittest.TestCase):
    def setUp(self):fresh();self.db=Db()
    def tearDown(self):self.db.close()
    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValidationError):A.SearchBody(query='x',mode='bogus')
        self.assertEqual(A.SearchBody(query='x').mode,'agent')
    def test_web_mode_gating(self):
        reader=User(username='r',password_hash='x',role='reader');admin=User(username='a',password_hash='x',role='admin')
        body=A.SearchBody(query='capital of france',mode='web')
        with mock.patch.object(A,'run_extension'),mock.patch.object(A,'web_results') as wr:
            with self.assertRaises(HTTPException) as c:run_search(body,reader,self.db)
            self.assertEqual(c.exception.status_code,403);wr.assert_not_called()
            with self.assertRaises(HTTPException) as c2:run_search(body,admin,self.db)   # no admin bypass
            self.assertEqual(c2.exception.status_code,403);wr.assert_not_called()
    def test_web_flag_off_by_default_never_routes_web(self):
        self.assertFalse(A.SearchBody(query='x').web)
        reader=User(username='r',password_hash='x',role='reader')
        with mock.patch.dict(os.environ,{'WEB_SEARCH_ENABLED':'true'}),mock.patch.object(A,'run_extension'),mock.patch.object(A,'web_results') as wr,mock.patch.object(A,'embedder') as emb,mock.patch.object(A,'vectors') as vec,mock.patch.object(A.lexical,'search',return_value=[]):
            emb.encode_query.return_value=[0.0];vec.search.return_value=[]
            r=run_search(A.SearchBody(query='capital of australia'),reader,self.db)   # org allows web, user did not switch it on
            wr.assert_not_called();self.assertEqual(r['route'],'local')
        with mock.patch.dict(os.environ,{'WEB_SEARCH_ENABLED':'false'}),mock.patch.object(A,'run_extension'):
            with self.assertRaises(HTTPException):run_search(A.SearchBody(query='x',web=True),reader,self.db)
    def test_default_disabled_and_admin_setting(self):
        self.assertFalse(A.web_enabled(self.db))
        with mock.patch.dict(os.environ,{'WEB_SEARCH_ENABLED':'true'}):self.assertTrue(A.web_enabled(self.db))

class FakeEmbedder:
    def encode(self,texts):return [[0.1,0.2,0.3] for _ in texts]
class FailEmbedder:
    def encode(self,texts):raise RuntimeError('boom')
class FakeStore:
    def __init__(self):self.points={};self.removed=[]
    def upsert(self,pts):
        for p in pts:self.points[p.id]=p.payload['doc_id']
    def remove_doc_except(self,doc_id,keep):
        for k in [k for k,v in self.points.items() if v==doc_id and k not in set(keep)]:del self.points[k]
    def remove_doc(self,doc_id):
        self.removed.append(doc_id)
        for k in [k for k,v in self.points.items() if v==doc_id]:del self.points[k]

class IngestTests(unittest.TestCase):
    def setUp(self):fresh();self.db=Db();self.up=Path(tempfile.mkdtemp());self.src=Path(tempfile.mkdtemp())
    def tearDown(self):self.db.close()
    def write(self,name,text):p=self.src/name;p.write_text(text);return p
    def test_register_dedup_and_versioning(self):
        p=self.write('policy.txt','Leave policy text that is long enough to be chunked properly.')
        d1=ingestion.register_file(self.db,p,self.up);self.assertEqual(d1.version,1)
        self.assertEqual(ingestion.register_file(self.db,p,self.up).id,d1.id)
        p=self.write('policy.txt','Changed leave policy text that is also long enough to be chunked.')
        d2=ingestion.register_file(self.db,p,self.up);self.assertEqual((d2.version,d2.id==d1.id),(2,False))
        self.assertEqual(self.db.query(Doc).count(),2)
        store=FakeStore();ingestion.ingest_document(self.db,d1.id,FakeEmbedder(),store);ingestion.ingest_document(self.db,d2.id,FakeEmbedder(),store)
        self.assertEqual([d.id for d in self.db.query(Doc).all()],[d2.id]);self.assertEqual(set(store.points.values()),{d2.id})
    def test_ingest_idempotent(self):
        d=ingestion.register_file(self.db,self.write('a.txt','Some reasonably long document body for the idempotency check test.'),self.up);store=FakeStore()
        r1=ingestion.ingest_document(self.db,d.id,FakeEmbedder(),store);ids1=sorted(c.id for c in self.db.query(Chunk).all());pts1=dict(store.points)
        r2=ingestion.ingest_document(self.db,d.id,FakeEmbedder(),store);ids2=sorted(c.id for c in self.db.query(Chunk).all())
        self.assertEqual((r1['status'],r2['status']),('ready','ready'));self.assertEqual(ids1,ids2);self.assertEqual(pts1,store.points);self.assertEqual(self.db.get(Doc,d.id).status,'ready')
    def test_failed_reingest_keeps_ready_doc_searchable(self):
        d=ingestion.register_file(self.db,self.write('a.txt','Some reasonably long document body for the failure check test.'),self.up);store=FakeStore()
        ingestion.ingest_document(self.db,d.id,FakeEmbedder(),store)
        with self.assertRaises(RuntimeError):ingestion.ingest_document(self.db,d.id,FailEmbedder(),store)
        self.assertEqual(self.db.get(Doc,d.id).status,'ready');self.assertGreater(self.db.query(Chunk).count(),0)
        d2=ingestion.register_file(self.db,self.write('b.txt','Another long enough body of text for a first-time failing ingest.'),self.up)
        with self.assertRaises(RuntimeError):ingestion.ingest_document(self.db,d2.id,FailEmbedder(),store)
        self.assertEqual(self.db.get(Doc,d2.id).status,'failed')
    def test_sweep_stale(self):
        add_doc(self.db,'old',['x'],status='processing');add_doc(self.db,'new',['x'],status='processing')
        self.db.get(Doc,'old').processing_started_at=datetime.now(timezone.utc)-timedelta(hours=2);self.db.get(Doc,'new').processing_started_at=datetime.now(timezone.utc);self.db.commit()
        self.assertEqual(ingestion.sweep_stale(self.db,30),1);self.assertEqual((self.db.get(Doc,'old').status,self.db.get(Doc,'new').status),('failed','processing'))

if __name__=='__main__':unittest.main()
