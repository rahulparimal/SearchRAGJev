import sys,unittest
sys.path.insert(0,'backend')
from pipeline import StructureChunker,estimate_tokens,pack_ranked_passages
from scripts.evaluate import metrics

class ChunkingTests(unittest.TestCase):
 def test_chunks_obey_limit_and_overlap(self):
  text=' '.join(f'token{i}' for i in range(1100))
  chunks=StructureChunker(max_words=120,overlap=20).split(text,4)
  self.assertGreater(len(chunks),5)
  self.assertTrue(all(len(c.split())<=120 for _,c in chunks))
  a=set(chunks[0][1].split());b=set(chunks[1][1].split());self.assertGreaterEqual(len(a&b),15)
 def test_token_counter_is_positive_and_monotonic(self):
  self.assertGreater(estimate_tokens('hello world'),0)
  self.assertGreater(estimate_tokens('hello world '*30),estimate_tokens('hello world'))
 def test_budgeted_context_is_smaller_than_retrieved_baseline(self):
  class Chunk:
   def __init__(self,i):self.id=str(i);self.doc_id='d';self.page=1;self.text=('Technical search evidence passage. '*100);self.token_estimate=400
  class Doc:id='d';title='Design';version=1
  rows=[(1.0-i*.1,Chunk(i),Doc()) for i in range(8)]
  packed,used,baseline,reduction=pack_ranked_passages(rows,1100,8)
  self.assertLessEqual(used,1100);self.assertLess(used,baseline);self.assertGreater(reduction,0);self.assertLessEqual(len(packed),8)
 def test_eval_metrics_reward_relevant_early_result(self):
  got=metrics([{'document_id':'a'},{'document_id':'b'},{'document_id':'c'}],['b','c'],3)
  self.assertEqual(got['recall_at_k'],1.0);self.assertAlmostEqual(got['mrr'],0.5);self.assertGreater(got['ndcg_at_k'],0.68)

if __name__=='__main__':unittest.main()
