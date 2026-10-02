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
_HEADING=re.compile(r'^(#{2,3})\s+(.*)')
_TABLE_ROW=re.compile(r'^\|(.*)\|\s*$')
_TABLE_SEP=re.compile(r'^[\s|:-]+$')
_BULLET=re.compile(r'^[-*]\s+(.*)')

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
    def __init__(self):super().__init__(claims=0,cited=0,dropped_uncited=0,dropped_out_of_range_only=0,table_rows_dropped=0,tables_dropped=0)

def _runs(text,n_sources,stats):
    """-> [{'text','cites'}], dropping any claim with no in-range citation unless it is a coverage-gap statement."""
    runs=[]
    for sent,ns in _claims(text):
        stats['claims']+=1
        in_range=[n for n in ns if 1<=n<=n_sources]
        if ns and not in_range:stats['dropped_out_of_range_only']+=1
        if not in_range and not sent.lower().startswith(COVERAGE_PREFIX):
            stats['dropped_uncited']+=1;continue
        stats['cited']+=1;runs.append({'text':sent,'cites':in_range})
    return runs

def parse_report(markdown,n_sources):
    """-> {'blocks':[...],'stats':{...},'drop_rate':float}. The report's title comes from the plan stage (deepresearch.py),
    NOT from sniffing the body: the body always starts with a real '## Summary' heading block like any other section.
    blocks: {'t':'h2'|'h3','text':str}
          | {'t':'p','runs':[{'text','cites'}]}            (empty runs -> block omitted)
          | {'t':'ul','items':[[{'text','cites'}],...]}
          | {'t':'table','head':[str],'rows':[[{'text','cites'}]]}  (a row with zero citations anywhere in it is dropped;
            a table left with fewer than 2 data rows is dropped entirely)
    A sentence/item with no in-range [S#] citation is dropped, UNLESS it starts with "The gathered sources do not cover"
    (a true statement about absence of evidence, not a factual claim). drop_rate = dropped_uncited / claims (0 if no claims)."""
    stats=_Stats();blocks=[]
    lines=(markdown or '').replace('\r\n','\n').replace('\r','\n').split('\n')
    para=[];table_head=None;table_rows=None;ul_items=None
    def flush_para():
        nonlocal para
        if para:
            runs=_runs(' '.join(para),n_sources,stats)
            if runs:blocks.append({'t':'p','runs':runs})
        para=[]
    def flush_ul():
        nonlocal ul_items
        if ul_items:blocks.append({'t':'ul','items':ul_items})
        ul_items=None
    def flush_table():
        nonlocal table_head,table_rows
        if table_head is not None:
            if table_rows and len(table_rows)>=2:blocks.append({'t':'table','head':table_head,'rows':table_rows})
            else:stats['tables_dropped']+=1
        table_head=None;table_rows=None
    for raw in lines:
        line=_clean(raw)
        if not line:
            flush_para();flush_ul();flush_table();continue
        hm=_HEADING.match(line)
        if hm:
            flush_para();flush_ul();flush_table()
            lvl='h2' if len(hm.group(1))==2 else 'h3'
            blocks.append({'t':lvl,'text':hm.group(2).strip()})
            continue
        tm=_TABLE_ROW.match(line)
        if tm:
            flush_para();flush_ul()
            if _TABLE_SEP.match(tm.group(1)):continue  # the |---|---| separator row
            cells=[c.strip() for c in tm.group(1).split('|')]
            if table_head is None:
                table_head=cells;table_rows=[]
            else:
                row=[]
                for c in cells:
                    ns=sorted({n for n in (int(x) for x in CITE.findall(c)) if 1<=n<=n_sources})
                    row.append({'text':c,'cites':ns})
                if any(c['cites'] for c in row):table_rows.append(row)
                else:stats['table_rows_dropped']+=1
            continue
        flush_table()
        bm=_BULLET.match(line)
        if bm:
            flush_para()
            runs=_runs(bm.group(1),n_sources,stats)
            if runs:
                if ul_items is None:ul_items=[]
                ul_items.append(runs)
            continue
        flush_ul();para.append(line)
    flush_para();flush_ul();flush_table()
    drop_rate=(stats['dropped_uncited']/stats['claims']) if stats['claims'] else 0.0
    return {'blocks':blocks,'stats':dict(stats),'drop_rate':drop_rate}

def to_markdown(parsed,title=None):
    """Reconstruct a clean markdown string from the validated AST (for the 'Copy' action) -- never the raw model output.
    `title` (from the plan stage) is rendered as an H1 if given."""
    out=[]
    if title:out.append(f"# {title}")
    for b in parsed['blocks']:
        if b['t'] in ('h2','h3'):out.append(('## ' if b['t']=='h2' else '### ')+b['text'])
        elif b['t']=='p':out.append(' '.join(r['text'] for r in b['runs']))
        elif b['t']=='ul':out.extend('- '+' '.join(r['text'] for r in item) for item in b['items'])
        elif b['t']=='table':
            out.append('| '+' | '.join(b['head'])+' |');out.append('|'+'|'.join('---' for _ in b['head'])+'|')
            out.extend('| '+' | '.join(c['text'] for c in row)+' |' for row in b['rows'])
    return '\n\n'.join(out)
