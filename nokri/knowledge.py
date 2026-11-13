"""Offline import: all raw sources are quarantined until reviewed, never auto-public."""
from contextlib import nullcontext
import gzip
import hashlib
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT={'.txt','.md','.wiki','.csv','.tsv','.json','.html'}

def extract(path):
    if path.suffix.lower() in TEXT: return path.read_text(encoding='utf-8-sig')
    if path.suffix.lower()=='.docx':
        from docx import Document
        d=Document(path)
        return '\n'.join([p.text for p in d.paragraphs]+[' | '.join(c.text for c in r.cells) for t in d.tables for r in t.rows])
    if path.suffix.lower()=='.pdf':
        from pypdf import PdfReader
        return '\n'.join(p.extract_text() or '' for p in PdfReader(path).pages)
    if path.suffix.lower()=='.xml':
        root=ElementTree.parse(path).getroot()
        return '\n'.join(x.text or '' for x in root.iter() if x.tag.split('}')[-1]=='text')
    return None

def put(db,id,source,text,event_date,public_from,access='restricted',status='draft',kind='canon',revision=1,connection=None):
    if access not in ('public','restricted','secret') or status not in ('approved','draft','generated'): raise ValueError('Некорректные метаданные')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',event_date) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',public_from): raise ValueError('Дата YYYY-MM-DD обязательна')
    with (nullcontext(connection) if connection is not None else db.transaction()) as c:
        old=c.execute('SELECT rowid FROM knowledge WHERE id=?',(id,)).fetchone()
        if old: c.execute('DELETE FROM knowledge_fts WHERE rowid=?',(old[0],))
        c.execute('INSERT OR REPLACE INTO knowledge VALUES(?,?,?,?,?,?,?,?,?)',(id,source,text,event_date,public_from,access,status,kind,revision))
        rowid=c.execute('SELECT rowid FROM knowledge WHERE id=?',(id,)).fetchone()[0]
        c.execute('INSERT INTO knowledge_fts(rowid,id,text) VALUES(?,?,?)',(rowid,id,text))

def import_tree(db,root):
    root=Path(root); report=[]
    for p in sorted(root.rglob('*')):
        if not p.is_file(): continue
        source=str(p.relative_to(root))
        try:
            text=extract(p)
            if text is None:
                report.append({'source':source,'status':'not_text','bytes':p.stat().st_size}); continue
            chunks=[text[i:i+1800] for i in range(0,len(text),1800)]
            years=[int(x) for x in re.findall(r'(?<!\d)(?:1\d{3}|20\d{2}|21\d{2})(?!\d)',text+' '+source)]
            conservative=f'{max(years):04}-12-31' if years else '9999-12-31'
            with db.transaction() as conn:
                for i,chunk in enumerate(chunks):
                    id=hashlib.sha256((source+':'+str(i)).encode()).hexdigest()[:24]
                    put(db,id,source,chunk,conservative,conservative,connection=conn)
            report.append({'source':source,'status':'quarantined','chunks':len(chunks)})
        except Exception as e:
            report.append({'source':source,'status':'error','error_type':type(e).__name__})
    return report

def load_curated(db,path):
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as source, db.transaction() as conn:
        for line in source:
            if line.strip(): put(db,**json.loads(line),connection=conn)
