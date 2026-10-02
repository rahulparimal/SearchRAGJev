import sys,unittest
sys.path.insert(0,'backend')
from research_extractive import best_sentences,build_extractive

TEXT=("Home page navigation and cookie notice for the site. Elasticsearch scales horizontally with sharding and replication across many nodes. "
      "Our newsletter is sent weekly to all subscribers worldwide. PostgreSQL full text search relies on GIN indexes inside the database.")
class ExtractiveTests(unittest.TestCase):
    def test_picks_matching_sentences_in_document_order(self):
        out=best_sentences(TEXT,'elasticsearch sharding nodes postgresql indexes',2)
        self.assertEqual(len(out),2);self.assertIn('Elasticsearch',out[0]);self.assertIn('PostgreSQL',out[1])
    def test_no_overlap_returns_nothing(self):self.assertEqual(best_sentences(TEXT,'quantum chromodynamics',2),[])
    def test_build_groups_by_subquestion_with_citations_and_dedupes(self):
        subqs=[{'question':'How does Elasticsearch scale with sharding?'},{'question':'What indexes does PostgreSQL use?'}]
        nums=[{'n':1,'subq':'Q1','text':TEXT},{'n':2,'subq':'Q1','text':TEXT},{'n':3,'subq':'Q2','text':TEXT}]
        r=build_extractive(subqs,nums)
        heads=[b['text'] for b in r['blocks'] if b['t']=='h2'];self.assertIn('How does Elasticsearch scale with sharding?',heads)
        q1=[b for b in r['blocks'] if b['t']=='ul'][0];self.assertEqual(len(q1['items']),1)  # the duplicate from S2 is dropped
        self.assertEqual(q1['items'][0][0]['cites'],[1]);self.assertIn('[S1]',q1['items'][0][0]['text'])
        self.assertEqual(r['drop_rate'],0.0);self.assertGreaterEqual(r['passages'],2)
    def test_no_passages_when_nothing_matches(self):
        self.assertEqual(build_extractive([{'question':'quantum chromodynamics'}],[{'n':1,'subq':'Q1','text':TEXT}])['passages'],0)
if __name__=='__main__':unittest.main()
