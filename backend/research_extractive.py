"""Deterministic 'extractive' research report: no model call. For each sub-question it quotes the best-matching sentences from the
gathered sources, each carrying its [S#] citation. It exists so a Deep Research run that found sources never ends as a bare error
when the writing model times out or fails: the user still gets the evidence, clearly labelled as NOT synthesised, with no
recommendations or conclusions invented. Output has the same block shape as research_report.parse_report()."""
import re
from bm25 import content_terms,tokenize
_SENT=re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"(\[])')

def _sentences(text):
    out=[]
    for s in _SENT.split(re.sub(r'\s+',' ',text or '')):
        s=s.strip()
        if 40<=len(s)<=400:out.append(s)
    return out

def best_sentences(text,query,k=2):
    """The k sentences of `text` with the most query-term overlap (ties keep document order); sentences with no overlap are never returned."""
    q=content_terms(query)
    if not q:return []
    scored=[]
    for i,s in enumerate(_sentences(text)):
        hits=len(q&set(tokenize(s)))
        if hits:scored.append((hits,-i,s))
    scored.sort(reverse=True)
    return [s for _,_,s in sorted(scored[:k],key=lambda x:-x[1])]

def build_extractive(subqs,numbered,per_source=2,per_subq=6):
    """subqs: [{'question',...}]; numbered: [{'n','subq','title','text','source'},...] -> {'blocks','stats','drop_rate'} like parse_report."""
    blocks=[{'t':'h2','text':'Summary (evidence only, not synthesised)'},
            {'t':'p','runs':[{'text':'The writing model did not finish a synthesised report, so this page shows the most relevant passages found for each question, '
                                      'quoted from the sources. Nothing here is a conclusion; check each passage against its source.','cites':[]}]}]
    claims=0
    for i,sq in enumerate(subqs):
        sid=f'Q{i+1}';items=[];seen=set()
        for src in numbered:
            if src.get('subq')!=sid:continue
            for s in best_sentences(src.get('text',''),sq['question'],per_source):
                key=s.lower()[:80]
                if key in seen:continue
                seen.add(key);items.append([{'text':f"{s} [S{src['n']}]",'cites':[src['n']]}])
        items=items[:per_subq]
        if not items:continue
        blocks.append({'t':'h2','text':sq['question']});blocks.append({'t':'ul','items':items});claims+=len(items)
    return {'blocks':blocks,'stats':{'claims':claims,'cited':claims,'dropped_uncited':0,'dropped_out_of_range_only':0,'table_rows_dropped':0,'tables_dropped':0},'drop_rate':0.0,'passages':claims}
