import unittest
from collections import namedtuple
from unittest import mock
import test_rag as T
import test_search_redesign as S
import app as A

C=namedtuple('C','id text page');D=namedtuple('D','title')
def sel(*texts):return [(1.0,C(f'c{i}',t,1),D(f'doc{i}')) for i,t in enumerate(texts)]

class ApplyFloorUnit(unittest.TestCase):
    def test_single_shared_term_is_not_enough(self):
        keep,_=A.apply_floor('kubernetes pod restart policy',sel('Our expense policy requires approval for travel.'),{},'rrf',{});self.assertEqual(keep,[False])
    def test_most_query_terms_present_is_kept(self):
        keep,_=A.apply_floor('parental leave weeks',sel('Parental leave is sixteen weeks of paid time off.'),{},'rrf',{});self.assertEqual(keep,[True])
    def test_three_matching_terms_are_kept_even_in_a_long_query(self):
        keep,_=A.apply_floor('how do I configure the vpn client to reconnect on windows laptops after sleep',sel('Configure the vpn client; it reconnects on windows after resume.'),{},'rrf',{})
        self.assertEqual(keep,[True])
    def test_question_words_do_not_dilute_coverage(self):
        keep,_=A.apply_floor('what is the capital of australia',sel('Canberra is the capital of Australia and its seat of government.'),{},'rrf',{});self.assertEqual(keep,[True])
    def test_jev_relevance_keeps_a_paraphrase_with_no_term_overlap(self):
        s=sel('Removable storage must be encrypted.');keep,_=A.apply_floor('can I plug in a thumb drive',s,{'c0':0.8},'jev_noul',{});self.assertEqual(keep,[True])
        keep,_=A.apply_floor('can I plug in a thumb drive',s,{'c0':0.1},'jev_noul',{});self.assertEqual(keep,[False])
    def test_cross_encoder_logit_threshold(self):
        s=sel('Unrelated text about lunch menus.');low=(-8+12)/16;high=(2+12)/16
        self.assertEqual(A.apply_floor('quarterly revenue forecast',s,{'c0':low},'cross_encoder',{})[0],[False]);self.assertEqual(A.apply_floor('quarterly revenue forecast',s,{'c0':high},'cross_encoder',{})[0],[True])
    def test_dense_similarity_alone_can_keep_a_paraphrase(self):
        self.assertEqual(A.apply_floor('thumb drive rules',sel('Removable storage must be encrypted.'),{},'rrf',{'c0':0.5})[0],[True])
        self.assertEqual(A.apply_floor('thumb drive rules',sel('Removable storage must be encrypted.'),{},'rrf',{'c0':0.1})[0],[False])
    def test_query_with_only_question_words_keeps_everything(self):
        self.assertEqual(A.apply_floor('what is this',sel('anything at all here'),{},'rrf',{})[0],[True])

class SearchFloor(unittest.TestCase):
    class H:
        def __init__(s,cid,doc,score):s.payload={'chunk_id':cid,'doc_id':doc,'page':1};s.score=score
    def setUp(self):S.local_env(self)
    def run_q(self,q,hits=(),**kw):
        A.vectors.search.return_value=list(hits)
        with mock.patch.object(A,'jev_client',return_value=S.FakeJev(enabled=False)):return S.aio(A.search(A.SearchBody(query=q,mode='local',**kw),T.Response(),S.reader(),self.db))
    def test_out_of_corpus_query_with_one_shared_word_returns_an_honest_empty_state(self):
        r=self.run_q('annual leave kubernetes autoscaling cluster')   # shares only 'annual leave' with the HR doc
        self.assertEqual(r['results'],[]);self.assertEqual(r['empty_reason'],'no_confident_local_match');self.assertGreater(r['suppressed'],0)
    def test_in_corpus_query_is_unaffected(self):
        r=self.run_q('parental leave sixteen weeks');self.assertEqual(r['results'][0]['document_id'],'hr');self.assertTrue(all('Parental' in x['text'] for x in r['results']))  # the annual-leave chunk shares only 'leave' and is dropped
    def test_low_confidence_flag_returns_flagged_passages(self):
        r=self.run_q('annual leave kubernetes autoscaling cluster',low_confidence=True)
        self.assertTrue(r['results']);self.assertTrue(all(x.get('low_confidence') for x in r['results']))
    def test_explain_is_admin_only(self):
        r=self.run_q('annual leave kubernetes autoscaling cluster',explain=True);self.assertNotIn('floor',r['usage'])
if __name__=='__main__':unittest.main()
