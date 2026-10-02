import sys,unittest
sys.path.insert(0,'backend')
from pipeline import StructureChunker,estimate_tokens,pack_ranked_passages
from security import validate_app_secret
from scripts.evaluate import metrics

class ChunkingTests(unittest.TestCase):
 def test_chunks_obey_limit_and_overlap(self):
  text=' '.join(f'token{i}' for i in range(1100))
  chunks=StructureChunker(max_words=120,overlap=20).split(text,4)
  self.assertGreater(len(chunks),5)
  self.assertTrue(all(len(c.split())<=120 for _,c in chunks))
  a=set(chunks[0][1].split());b=set(chunks[1][1].split());self.assertGreaterEqual(len(a&b),15)
 def test_chunk_boundaries_never_fall_mid_sentence(self):
  """A PDF line-wrap (single \\n) must not force a chunk boundary mid-sentence; only a real sentence end should."""
  sentences=[f'This is sentence number {i} about the ancient texts and their many teachings.' for i in range(40)]
  # join with a mix of single newlines (typeset line wraps) at arbitrary points, simulating pypdf's per-line extraction
  text='\n'.join(sentences)
  chunks=StructureChunker(max_words=60,overlap=15).split(text,1)
  self.assertGreater(len(chunks),3)
  for _,c in chunks:
   self.assertTrue(c.strip().endswith('.'),f'chunk does not end on a sentence boundary: {c[-40:]!r}')
   self.assertTrue(c.strip()[0].isupper(),f'chunk does not start on a sentence boundary: {c[:40]!r}')
 def test_short_paragraphs_are_packed_together_not_fragmented(self):
  """Many short verse-like paragraphs (blank-line separated) below max_words should be packed into shared chunks, not
  each becoming its own tiny chunk -- the buffer must not reset at every paragraph boundary."""
  verses='\n\n'.join(f'{i}. This is verse number {i} of the text, a short teaching on dharma.' for i in range(20))
  chunks=StructureChunker(max_words=60,overlap=10).split(verses,1)
  self.assertLess(len(chunks),10)  # packed together, not ~20 one-verse chunks
  self.assertTrue(all(len(c.split())<=60 for _,c in chunks))
 def test_split_document_does_not_cut_a_sentence_at_a_page_boundary(self):
  pages=[(1,'This sentence starts on page one and'),(2,'continues onto page two before it ends.')]
  chunks=StructureChunker(max_words=100,overlap=10).split_document(pages)
  self.assertEqual(len(chunks),1)
  page,text=chunks[0]
  self.assertEqual(page,1)  # starting page
  self.assertIn('starts on page one and continues onto page two before it ends.',text)
 def test_overlap_cannot_make_next_paragraph_exceed_limit(self):
  text=' '.join(f'first{i}' for i in range(120))+'\n'+' '.join(f'second{i}' for i in range(115))
  chunks=StructureChunker(max_words=120,overlap=20).split(text,1)
  self.assertTrue(all(len(content.split())<=120 for _,content in chunks))
  self.assertIn('second114',chunks[-1][1])
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
 def test_budget_is_respected_near_boundary(self):
  class Chunk:
   id='c';page=1;text=' '.join(f'x{i}' for i in range(150));token_estimate=1
  class Doc:id='d';title='Design';version=1
  packed,used,_,_=pack_ranked_passages([(1.0,Chunk(),Doc())],30,1)
  self.assertLessEqual(used,30)
  self.assertEqual(used,estimate_tokens(packed[0]['text']))
 def test_eval_metrics_reward_relevant_early_result(self):
  got=metrics([{'document_id':'a'},{'document_id':'b'},{'document_id':'c'}],['b','c'],3)
  self.assertEqual(got['recall_at_k'],1.0);self.assertAlmostEqual(got['mrr'],0.5);self.assertGreater(got['ndcg_at_k'],0.68)
 def test_eval_metrics_deduplicate_documents_and_stay_bounded(self):
  got=metrics([{'document_id':'a'},{'document_id':'a'},{'document_id':'b'},{'document_id':'b'}],['a','b'],4)
  self.assertEqual(got['recall_at_k'],1.0);self.assertEqual(got['mrr'],1.0);self.assertEqual(got['ndcg_at_k'],1.0)
  for key in ('recall_at_k','mrr','ndcg_at_k'):
   self.assertGreaterEqual(got[key],0);self.assertLessEqual(got[key],1)
  self.assertEqual(metrics([{'document_id':'a'}],['a'],-3)['recall_at_k'],0)
 def test_signing_secret_rejects_examples_and_short_values(self):
  for value in ('','local-dev-secret-change-me','replace-with-a-long-random-secret','tiny'):
   with self.subTest(value=value),self.assertRaises(RuntimeError):validate_app_secret(value)
  validate_app_secret('1d490fa5838a566d46d3aef1da4358c97ca006eb0cfd70527bd9c35d3afcb112')

if __name__=='__main__':unittest.main()
