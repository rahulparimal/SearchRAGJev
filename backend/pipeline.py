"""Deterministic ingestion and evidence budgeting; safe to test without infrastructure."""
from __future__ import annotations
import io,re
from pathlib import Path
from docx import Document as DocxDocument
from pypdf import PdfReader

def estimate_tokens(text:str)->int:
    # Cheap proxy (~0.75 words/token); benchmark reports this as an estimate.
    return max(1,int(len(re.findall(r'\w+|[^\w\s]',text))/0.75))

_DEHYPHEN=re.compile(r'(\w)-\n(\w)')
_OCR_DIACRITIC=re.compile(r'[$?](?=[A-Za-z])')
def _clean_pdf_page(t:str)->str:
    """De-hyphenate words split by a line-break hyphen (typeset PDFs: 'ex-\\npiation' -> 'expiation'), and fix a common PDF-OCR
    failure on transliterated non-Latin text: Sanskrit diacritic letters (ś/ṣ/ṭ etc., as in Viṣṇu/Śiva) are frequently
    misrecognized as a literal '$' or '?' (measured: 500+ occurrences in a sample scanned Sanskrit text PDF). A genuine '$' or
    '?' is never immediately followed by a letter (real amounts are followed by a digit; real question marks by whitespace/
    quote/paren), so this is a safe, high-confidence signal -- not a blanket substitution. Applied before any chunker sees the
    text, so it affects both the embedded vector and the stored/displayed text equally."""
    return _OCR_DIACRITIC.sub('s',_DEHYPHEN.sub(r'\1\2',t))
class DocumentExtractor:
    def extract(self,data:bytes,filename:str)->list[tuple[int,str]]:
        suffix=Path(filename).suffix.lower()
        if suffix=='.pdf':return [(i+1,_clean_pdf_page((p.extract_text() or '').strip())) for i,p in enumerate(PdfReader(io.BytesIO(data)).pages)]
        if suffix=='.docx':
            d=DocxDocument(io.BytesIO(data)); blocks=[p.text.strip() for p in d.paragraphs if p.text.strip()]
            blocks += [' | '.join(c.text.strip() for c in row.cells) for table in d.tables for row in table.rows]
            return [(1,'\n'.join(blocks))]
        if suffix in {'.txt','.md','.html'}:
            text=data.decode('utf-8',errors='replace')
            if suffix=='.html':text=re.sub(r'<[^>]+>',' ',text)
            return [(1,text)]
        raise ValueError('Supported formats: PDF, DOCX, TXT, Markdown and HTML. PPTX is not enabled.')

_SENT_SPLIT=re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"(])')
class StructureChunker:
    """Sentence and paragraph aware bounded chunks with controlled overlap.
    A single `\\n` is a PDF/typeset LINE wrap (mid-sentence most of the time), not a paragraph break: it is joined into a
    space before sentence splitting. Only a blank line (`\\n` repeated, optionally with whitespace between) is a paragraph
    break. Units are packed as WHOLE sentences -- a flush only ever happens between sentences, never inside one -- and the
    overlap carried into the next chunk is whole trailing sentences (up to `overlap` words), not an arbitrary word slice.
    A single sentence/paragraph longer than max_words still falls back to a hard word-wrap (unavoidable without truncating
    the content), matching the previous behaviour for that case."""
    def __init__(self,max_words=420,overlap=65):
        if max_words<=0 or overlap<0 or overlap>=max_words:
            raise ValueError('max_words must be positive and overlap must be smaller than max_words')
        self.max_words=max_words;self.overlap=overlap
    def _units(self,text:str,page):
        """-> [(page,sentence), ...] for one page's raw text: collapse line-wraps, split on blank-line paragraph breaks, then sentences."""
        text=re.sub(r'\n\s*\n+',' ',text or '').replace('\n',' ')
        paras=[re.sub(r'[ \t]+',' ',p).strip() for p in text.split(' ') if p.strip()]
        return [(page,s.strip()) for para in paras for s in _SENT_SPLIT.split(para) if s.strip()]
    def _pack(self,units):
        """units: [(page,sentence), ...], already flattened across paragraphs (and pages, for split_document) so the
        buffer is never reset at a paragraph or page boundary -- only when it actually reaches max_words. A chunk's page
        is the page its FIRST sentence came from. Flush happens only between sentences, never inside one."""
        out=[];buf=[];n=0  # buf: list of (page,sentence)
        def flush(next_words=0):
            nonlocal buf,n
            if buf:out.append((buf[0][0],' '.join(s for _,s in buf)))
            budget=min(self.overlap,max(0,self.max_words-next_words));kept=[];kept_n=0
            for pg,s in reversed(buf):
                wc=len(s.split())
                if kept and kept_n+wc>budget:break
                kept.insert(0,(pg,s));kept_n+=wc
                if kept_n>=budget:break
            buf=kept;n=kept_n
        for pg,sent in units:
            words=sent.split()
            if len(words)>self.max_words:
                if buf:flush()
                buf=[];n=0
                for i in range(0,len(words),self.max_words-self.overlap):out.append((pg,' '.join(words[i:i+self.max_words])))
                continue
            if n+len(words)>self.max_words:flush(len(words))
            buf.append((pg,sent));n+=len(words)
        if buf:out.append((buf[0][0],' '.join(s for _,s in buf)))
        return [x for x in out if len(x[1])>20]
    def split(self,text:str,page:int):
        return self._pack(self._units(text,page))
    def split_document(self,pages:list[tuple[int,str]]):
        """Like split(), but chunks the WHOLE document as one stream instead of once per page, so a sentence or short
        verse-like paragraph straddling a page break is never cut in two. Each chunk's `page` is where it starts."""
        units=[u for page,text in pages for u in self._units(text,page)]
        return self._pack(units)

def pack_ranked_passages(passages,budget:int,max_passages:int):
    """Pack ranked tuples(score, chunk, document) within a token proxy budget."""
    packed=[];used=0;seen=set();naive=max(1,sum(p[1].token_estimate for p in passages[:max_passages]))
    for score,chunk,doc in passages:
        fingerprint=re.sub(r'\W+',' ',chunk.text.lower())[:600]
        if fingerprint in seen:continue
        seen.add(fingerprint)
        remain=budget-used
        if remain<=0 or len(packed)>=max_passages:break
        content=chunk.text
        if estimate_tokens(content)>remain:
            words=content.split();low=0;high=len(words)
            while low<high:
                mid=(low+high+1)//2
                if estimate_tokens(' '.join(words[:mid]))<=remain:low=mid
                else:high=mid-1
            content=' '.join(words[:low])
        tokens=estimate_tokens(content)
        if tokens<20:continue
        packed.append({'rank':len(packed)+1,'score':round(float(score),4),'document_id':doc.id,'title':doc.title,'version':doc.version,'page':chunk.page,'chunk_id':chunk.id,'text':content,'snapshot_url':f'/documents/{doc.id}/snapshot?page={chunk.page}'})
        used+=tokens
    return packed,used,naive,max(0,round(100*(1-used/naive),1))
