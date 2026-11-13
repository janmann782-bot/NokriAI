"""Import public Aurelia Wiki prose into dated, source-traceable search entries."""
from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

WIKI_EXPORT_DAY = '2061-11-12'
DOC_KIND = 'canon'
# Earlier public Wiki revisions sometimes disagree with direct author-approved
# data. Skip specific known stale records instead of rewriting the source.
STALE_INFOBOX_FIELDS = {'Кефирстан': {'площадь'}}
STALE_VALUES = {'2 320 032'}
_SKIPPED_FIELDS = re.compile(r'^(?:флаг|герб|эмблема|изображение|логотип|фото|схема|цвет|фон|стиль|карта|карточка|координаты|подпись)(?:_|$)',re.I)
_UNAVAILABLE_WORDS = re.compile(r'(?<!\w)(?:русск\w*|украинск\w*|российск\w*|russian|ukrainian|earth)(?!\w)',re.I)
_SENSITIVE = re.compile(r'(?i)совершенно\s+секретн|государственн\w*\s+тайн\w*|засекреченн\w*\s+(?:документ|операц|план)|непубличн\w*\s+данн\w*')
_YEAR = re.compile(r'(?<!\d)(?:1[89]\d{2}|20\d{2}|21\d{2})(?!\d)')
_ISO_DATE = re.compile(r'(?<!\d)(20\d{2}|21\d{2})-(\d\d)-(\d\d)(?!\d)')
_DMY_DATE = re.compile(r'(?<!\d)(\d{1,2})[.]\s*(\d{1,2})[.]\s*(20\d{2}|21\d{2})(?!\d)')


def clean_markup(value: str) -> str:
    """Convert common Wiki formatting to text without executing templates."""
    t = html.unescape(value)
    t = re.sub(r'<!--.*?-->', ' ', t, flags=re.S)
    t = re.sub(r'<ref\b[^>]*>.*?</ref\s*>|<ref\b[^>]*/>', ' ', t, flags=re.I|re.S)
    t = re.sub(r'\[\[(?:File|Файл|Category|Категория):[^]]+\]\]', ' ', t, flags=re.I)
    t = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', t)
    t = re.sub(r'\[\[([^\]]+)\]\]', r'\1', t)
    for _ in range(6):
        t2 = re.sub(r'\{\{[^{}]*\}\}', ' ', t,flags=re.S)
        if t2==t: break
        t=t2
    t = re.sub(r'<[^>]*>', ' ', t)
    t = re.sub(r'\[(https?://\S+)(?:\s+([^]]+))?\]', lambda m: m[2] or ' ',t)
    t = t.replace("'''",'').replace("''",'').replace('__NOTOC__','')
    t = re.sub(r'\{\|.*?\|\}', ' ', t,flags=re.S) if len(t)<2000 else t
    t = re.sub(r'[\t ]+', ' ', t)
    t = re.sub(r'\s*\n\s*', ' ', t)
    return t.strip(' :-|!\t')


def date_visibility(text: str, title: str) -> str:
    years = [int(x) for x in _YEAR.findall(text+' '+title)]
    if not years:
        return WIKI_EXPORT_DAY
    maximum = max(years)
    if maximum > 2061:
        # A year without a date does not establish that the event already
        # happened at the beginning of that year.
        assumed = f'{maximum}-12-31'
    else:
        assumed = WIKI_EXPORT_DAY
    dates=[]
    for y,m,d in _ISO_DATE.findall(text+' '+title):
        if 1<=int(m)<=12 and 1<=int(d)<=31:
            dates.append(f'{y}-{m}-{d}')
    for d,m,y in _DMY_DATE.findall(text+' '+title):
        if 1<=int(m)<=12 and 1<=int(d)<=31:
            dates.append(f'{y}-{int(m):02}-{int(d):02}')
    if dates:
        assumed=max(assumed,*dates)
    return assumed


def _safe_fragment(t: str)->bool:
    return bool(t) and not _UNAVAILABLE_WORDS.search(t) and not _SENSITIVE.search(t)


def wiki_records(xml_text: str):
    root=ET.fromstring(xml_text)
    ns={'m':'http://www.mediawiki.org/xml/export-0.11/'}
    for page in root.findall('m:page',ns):
        title=page.findtext('m:title',default='',namespaces=ns)
        if int(page.findtext('m:ns',default='-1',namespaces=ns))!=0:
            continue
        raw=page.findtext('m:revision/m:text',default='',namespaces=ns)
        if not raw or re.match(r'^\s*#REDIRECT\b',raw,re.I):
            continue
        paragraphs=[]
        for line in raw.splitlines():
            line=line.strip()
            if not line or line.startswith(('{{','}}','<!--','[[Категория:','#REDIRECT','{#','{|','|}','|-','<div','</div','<span','</span','<br')):
                continue
            if line.startswith('|'):
                match=re.fullmatch(r'\|\s*([\wа-яА-ЯёЁ]+)\s*=\s*(.+)',line)
                if not match: continue
                key,value=match.groups()
                if key.lower() in STALE_INFOBOX_FIELDS.get(title,set()): continue
                if _SKIPPED_FIELDS.search(key): continue
                value=clean_markup(value)
                if len(value)<2 or len(value)>500 or not _safe_fragment(value): continue
                paragraphs.append(f'{title}. {key.replace("_"," ")}: {value}')
                continue
            if line.startswith(('!',';')): continue
            if line.startswith(('==','=')) and line.endswith('='):
                continue
            if line.startswith(('*','#')):
                line=line.lstrip('*# ')
            text=clean_markup(line)
            if title in {'Кефирстан','Йогуртстанская война'} and any(v in text for v in STALE_VALUES):
                continue
            if len(text)<35 or len(text)>3000 or not _safe_fragment(text):
                continue
            paragraphs.append(f'{title}. {text}')
        for i,paragraph in enumerate(paragraphs):
            visibility=date_visibility(paragraph,title)
            yield {
                'id':'wiki-'+hashlib.sha256((title+'|'+str(i)+'|'+paragraph).encode()).hexdigest()[:22],
                'source':'Aurelia Wiki/'+title,
                'text':paragraph[:1800],
                'event_date':visibility,
                'public_from':max(visibility,WIKI_EXPORT_DAY),
                'access':'public',
                'status':'approved',
                'kind':DOC_KIND,
                'revision':1,
            }


def read_packaged_wiki() -> str:
    folder=Path(__file__).resolve().parent/'resources'/'wiki_xml'
    paths=sorted(folder.glob('part_*.part'))
    if len(paths)!=39:
        raise FileNotFoundError('Incomplete public wiki corpus: expected 39 archive fragments')
    joined=[]
    for p in paths:
        fragment=p.read_text(encoding='utf-8')
        if not fragment: raise ValueError('Empty wiki archive fragment')
        joined.append(fragment if fragment.endswith('\n') else fragment+'\n')
    raw=''.join(joined)
    if not raw.startswith('<?xml') or not raw.rstrip().endswith('</mediawiki>'):
        raise ValueError('Corrupt XML export corpus')
    return raw


def import_packaged_wiki(db):
    from .knowledge import put
    data=read_packaged_wiki()
    count=0
    with db.transaction() as conn:
        for row in wiki_records(data):
            put(db,**row,connection=conn)
            count+=1
    return count
