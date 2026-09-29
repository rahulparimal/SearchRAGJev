"""TypeSafe Jev decision and optional relevance-score reranking adapters."""
import json,math,os,httpx

class JevDecisionProvider:
    def __init__(self,token_provider,model_provider=None,base_url_provider=None):
        self.token_provider=token_provider;self.model_provider=model_provider;self.base_url_provider=base_url_provider
    def decide(self,state,questions):
        token=self.token_provider()
        if not token:return {}
        content=state if isinstance(state,str) else json.dumps(state,ensure_ascii=False)
        try:
            model=(self.model_provider() if self.model_provider else '') or os.getenv('JEV_MODEL','jev-latest')
            base_url=(self.base_url_provider() if self.base_url_provider else '') or os.getenv('JEV_BASE_URL','https://api.typesafe.ai')
            response=httpx.post(base_url.rstrip('/')+'/v1/systemone',headers={'Authorization':f'Bearer {token}'},json={'model':model,'state':content[:6000],'questions':questions},timeout=2.0)
            response.raise_for_status();return response.json().get('answers',{})
        except (httpx.HTTPError,ValueError,TypeError,KeyError):return {}

class JevScoreReranker:
    """One SystemOne request with typed Score questions for a bounded candidate set."""
    def __init__(self,decision_provider):self.decisions=decision_provider
    def rerank(self,query,passages,max_candidates=8):
        bounded=passages[:max_candidates]
        if len(bounded)<2:return passages,'rrf'
        # Keep all candidate evidence within the decision provider's 6,000-character state limit.
        candidates=[{'key':f'c{i}','title':doc.title[:100],'page':chunk.page,'text':chunk.text[:400]} for i,(_,chunk,doc) in enumerate(bounded)]
        questions={item['key']:{'type':'score','instructions':f"Rate how well candidate {item['key']} directly answers the search query. Evaluate relevance, not writing quality.",'criteria':['Does not address the query','Related but does not directly answer','Directly answers or provides strong evidence']} for item in candidates}
        answers=self.decisions.decide({'query':query[:500],'candidates':candidates},questions)
        if len(answers)!=len(questions):return passages,'rrf_fallback'
        scored=[]
        for i,row in enumerate(bounded):
            answer=answers.get(f'c{i}',{});value=answer.get('score')
            if value is None:return passages,'rrf_fallback'
            try:score=float(value)
            except (TypeError,ValueError):return passages,'rrf_fallback'
            if not math.isfinite(score):return passages,'rrf_fallback'
            scored.append((score,row[0],row))
        scored.sort(key=lambda x:(x[0],x[1]),reverse=True)
        return [x[2] for x in scored]+passages[len(bounded):],'jev_score'
