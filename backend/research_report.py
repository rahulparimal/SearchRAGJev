"""Deterministic, side-effect-free parsing and validation of a Deep Research report body. No I/O, no LLM calls here --
easy to unit-test in isolation. The model is asked to write restricted markdown (## / ### headings, paragraphs, '- ' lists,
'|' pipe tables); this module turns that into a typed block AST and enforces that every factual claim carries an in-range
citation, dropping the rest -- so what gets rendered to the user is exactly what was validated, the same principle as
overview.py's clean_overview/parse/assemble. Markdown links, HTML tags and emphasis markers are stripped (never rendered
as HTML), and citation variants ([1], [Source 2], [S1,S2]) are canonicalised to [S#] form, mirroring overview.CITE_VARIANT.
"""
import re

_HTML_TAG=re.compile(r'<[^>]+>')
_MD_LINK=re.compile(r'\[([^\]]+)\]\([^)]*\)')
_MD_EMPH=re.compile(r'\*\*|__|`')
CITE_VARIANT=re.compile(r'\[\s*(?:S|Source)?\s*(\d+(?:\s*[,;]\s*(?:S|Source)?\s*\d+)*)\s*\]',re.I)
CITE=re.compile(r'\[S(\d+)\]')
COVERAGE_PREFIX='the gathered sources do not cover'
_SENT_SPLIT=re.compile(r'(?<=[.!?\]])\s+(?=[A-Z0-9"(])')
_HEADING=re.compile(r'^(#{2,4})\s+(.*)')
_TABLE_ROW=re.compile(r'^\|(.*)\|\s*$')
_TABLE_SEP=re.compile(r'^[\s|:-]+$')
_BULLET=re.compile(r'^[-*]\s+(.*)')
_ORDERED=re.compile(r'^\d+[.)]\s+(.*)')
_LABEL=re.compile(r'^\*\*([^*:]{2,60}?):?\*\*:?\s*(.*)$')  # **Label:** text  (checked on the RAW line, before emphasis is stripped)
# Sections where the model is ANALYSING (framing, interpretation, implications, recommendations) rather than reporting source facts: sentences there may
# be uncited, are flagged analysis=true, and must pass a deterministic new-fact check (no number or proper noun that is not in the sources or the question).
ANALYSIS_RE=re.compile(r'interpretation|limitation|implication|recommend|further research|questions? for|decision|scope|definition|scenario|not established|next step|confidence',re.I)
_NUM=re.compile(r'\d[\d,]*(?:\.\d+)?%?')
_PROPER=re.compile(r"\b[A-Z][A-Za-z0-9+#-]{2,}\b")

def _clean(text):
    """Strip markdown links/HTML/emphasis and canonicalise citation variants to [S#] form. Never produces HTML."""
    t=_MD_LINK.sub(r'\1',text or '')
    t=_HTML_TAG.sub('',t)
    t=_MD_EMPH.sub('',t)
    t=CITE_VARIANT.sub(lambda m:''.join(f'[S{n}]' for n in re.findall(r'\d+',m.group(1))),t)
    return re.sub(r'[ \t]+',' ',t).strip()

def _claims(text):
    """-> [(sentence, [cite_n, ...])] for one paragraph/list-item's cleaned text."""
    out=[]
    for sent in _SENT_SPLIT.split(text):
        sent=sent.strip()
        if sent:out.append((sent,sorted({int(x) for x in CITE.findall(sent)})))
    return out

class _Stats(dict):
    def __init__(self):super().__init__(claims=0,cited=0,dropped_uncited=0,dropped_out_of_range_only=0,table_rows_dropped=0,tables_dropped=0,analysis=0,analysis_dropped=0)

def new_facts_ok(sentence,corpus):
    """True when `sentence` introduces no number/year/percentage and no capitalised term (other than its first word) that is absent from `corpus`
    (lower-cased sources + question text). Deterministic guard so an uncited 'analysis' sentence cannot smuggle in a new fact."""
    nocomma=corpus.replace(',','')
    for n in _NUM.findall(re.sub(r'\[S\d+\]','',sentence)):
        if n.replace(',','').rstrip('%').rstrip('.') not in nocomma:return False
    words=_PROPER.findall(re.sub(r'\[S\d+\]','',sentence));first=sentence.split(' ',1)[0].strip('.,;:()"')
    for w in words:
        if w==first:continue
        if w.lower() not in corpus:return False
    return True

def _runs(text,n_sources,stats,analysis=False,corpus=None):
    """-> [{'text','cites'}], dropping any claim with no in-range citation unless it is a coverage-gap statement, or (in an ANALYSIS
    section, when a corpus is supplied) an uncited sentence that passes the new-fact check (kept, flagged analysis=true)."""
    runs=[]
    for sent,ns in _claims(text):
        in_range=[n for n in ns if 1<=n<=n_sources]
        if not in_range and not sent.lower().startswith(COVERAGE_PREFIX) and analysis and corpus is not None:
            if new_facts_ok(sent,corpus):stats['analysis']+=1;runs.append({'text':sent,'cites':[],'analysis':True})
            else:stats['analysis_dropped']+=1
            continue
        stats['claims']+=1
        if ns and not in_range:stats['dropped_out_of_range_only']+=1
        if not in_range and not sent.lower().startswith(COVERAGE_PREFIX):
            stats['dropped_uncited']+=1;continue
        stats['cited']+=1;runs.append({'text':sent,'cites':in_range})
    return runs

def parse_report(markdown,n_sources,analysis_corpus=None):
    """-> {'blocks':[...],'stats':{...},'drop_rate':float}. The report's title comes from the plan stage (deepresearch.py), NOT from sniffing the body.
    blocks: {'t':'h2'|'h3'|'h4','text':str}
          | {'t':'label','text':str}                          (a line that is only **Label:**)
          | {'t':'p','runs':[{'text','cites'[,'analysis']}][,'label':str]}   (empty runs -> block omitted; a leading **Label:** text is kept as 'label')
          | {'t':'ul'|'ol','items':[[{'text','cites'}],...]}
          | {'t':'table','head':[str],'rows':[[{'text','cites'}]][,'analysis':True]}  (a row with zero citations anywhere is dropped, unless the table sits
            in an analysis section and the row passes the new-fact check; fewer than 2 data rows -> table dropped)
    A sentence with no in-range [S#] citation is dropped, UNLESS it starts with "The gathered sources do not cover" or it is an analysis-section
    sentence that passes new_facts_ok (only when `analysis_corpus`, the lower-cased source+question text, is given). drop_rate = dropped_uncited / claims."""
    stats=_Stats();blocks=[];heading='';label=None
    lines=(markdown or '').replace('\r\n','\n').replace('\r','\n').split('\n')
    para=[];para_label=None;table_head=None;table_rows=None;table_analysis=False;ul_items=None;ol_items=None
    def in_analysis():return analysis_corpus is not None and bool(ANALYSIS_RE.search(heading) or ANALYSIS_RE.search(label or ''))
    def flush_para():
        nonlocal para,para_label
        if para:
            runs=_runs(' '.join(para),n_sources,stats,in_analysis(),analysis_corpus)
            if runs:
                b={'t':'p','runs':runs}
                if para_label:b['label']=para_label
                blocks.append(b)
        para=[];para_label=None
    def flush_list():
        nonlocal ul_items,ol_items
        if ul_items:blocks.append({'t':'ul','items':ul_items})
        if ol_items:blocks.append({'t':'ol','items':ol_items})
        ul_items=None;ol_items=None
    def flush_table():
        nonlocal table_head,table_rows,table_analysis
        if table_head is not None:
            if table_rows and len(table_rows)>=2:
                b={'t':'table','head':table_head,'rows':table_rows}
                if table_analysis:b['analysis']=True
                blocks.append(b)
            else:stats['tables_dropped']+=1
        table_head=None;table_rows=None;table_analysis=False
    for raw in lines:
        lm=_LABEL.match(raw.strip())
        line=_clean(raw)
        if not line:
            flush_para();flush_list();flush_table();continue
        hm=_HEADING.match(line)
        if hm:
            flush_para();flush_list();flush_table()
            lvl={2:'h2',3:'h3',4:'h4'}[len(hm.group(1))];heading=hm.group(2).strip();label=None
            blocks.append({'t':lvl,'text':heading})
            continue
        tm=_TABLE_ROW.match(line)
        if tm:
            flush_para();flush_list()
            if _TABLE_SEP.match(tm.group(1)):continue  # the |---|---| separator row
            cells=[c.strip() for c in tm.group(1).split('|')]
            if table_head is None:
                table_head=cells;table_rows=[]
            else:
                row=[]
                for c in cells:
                    ns=sorted({n for n in (int(x) for x in CITE.findall(c)) if 1<=n<=n_sources})
                    row.append({'text':c,'cites':ns})
                if any(c['cites'] for c in row):
                    rc=sorted({n for c in row for n in c['cites']})
                    for k,c in enumerate(row):  # an uncited value cell inherits its row's citations (and says so) so it still gets checked
                        if k>0 and not c['cites'] and c['text'].strip(' -\u2013\u2014') and rc:c['cites']=rc;c['rowcited']=True
                    table_rows.append(row)
                elif in_analysis() and new_facts_ok(' '.join(c['text'] for c in row if not re.fullmatch(r'\d{1,2}',c['text'].strip())),analysis_corpus):  # a bare 1-2 digit cell is a priority/index, not a fact
                    table_rows.append(row);table_analysis=True;stats['analysis']+=1
                else:stats['table_rows_dropped']+=1
            continue
        flush_table()
        if lm:  # **Label:** [text]
            flush_para();flush_list();label=lm.group(1).strip()
            rest=_clean(lm.group(2))
            if rest:para=[rest];para_label=label
            else:blocks.append({'t':'label','text':label})
            continue
        bm=_BULLET.match(line)
        if bm:
            flush_para()
            if ol_items:flush_list()
            runs=_runs(bm.group(1),n_sources,stats,in_analysis(),analysis_corpus)
            if runs:
                if ul_items is None:ul_items=[]
                ul_items.append(runs)
            continue
        om=_ORDERED.match(line)
        if om:
            flush_para()
            if ul_items:flush_list()
            runs=_runs(om.group(1),n_sources,stats,in_analysis(),analysis_corpus)
            if runs:
                if ol_items is None:ol_items=[]
                ol_items.append(runs)
            continue
        flush_list();para.append(line)
    flush_para();flush_list();flush_table()
    drop_rate=(stats['dropped_uncited']/stats['claims']) if stats['claims'] else 0.0
    return {'blocks':blocks,'stats':dict(stats),'drop_rate':drop_rate}

def to_markdown(parsed,title=None):
    """Reconstruct a clean markdown string from the validated AST (for the 'Copy' action) -- never the raw model output.
    `title` (from the plan stage) is rendered as an H1 if given."""
    out=[]
    if title:out.append(f"# {title}")
    def txt(runs):return ' '.join(r['text'] for r in runs)
    for b in parsed['blocks']:
        t=b['t']
        if t in ('h2','h3','h4'):out.append({'h2':'## ','h3':'### ','h4':'#### '}[t]+b['text'])
        elif t=='label':out.append(f"**{b['text']}:**")
        elif t=='p':out.append((f"**{b['label']}:** " if b.get('label') else '')+txt(b['runs']))
        elif t=='ul':out.extend('- '+txt(item) for item in b['items'])
        elif t=='ol':out.extend(f'{i}. '+txt(item) for i,item in enumerate(b['items'],1))
        elif t=='confidence':out.append(f"**Confidence: {b['level']}** - {b['reason']}")
        elif t=='table':
            out.append('| '+' | '.join(b['head'])+' |');out.append('|'+'|'.join('---' for _ in b['head'])+'|')
            out.extend('| '+' | '.join(c['text'] for c in row)+' |' for row in b['rows'])
    return '\n\n'.join(out)
