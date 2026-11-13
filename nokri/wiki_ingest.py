"""Index public Aurelia Wiki articles as date-filtered, source-attributed canon.

Only the explicitly public Wiki export is indexed automatically. Non-Wiki files
from the large source archive remain quarantined until approved by the author.
"""
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from io import StringIO
from pathlib import Path

SKIP_PREFIX = ('Шаблон:', 'Категория:', 'Файл:', 'Модуль:', 'MediaWiki:', 'Обсуждение:', 'Участник:')
EXTERNAL_NAMES = re.compile(r'(?i)(?<!\w)(?:россия|украина|сша|земля\s+как\s+планета|русский\s+язык|украинский\s+язык)(?!\w)')
YEAR = re.compile(r'(?<!\d)(?:1\d{3}|20\d{2}|21\d{2})(?!\d)')
DATE_ISO = re.compile(r'(?<!\d)((?:20|21)\d{2})[-.]([01]?\d)[-.]([0-3]?\d)(?!\d)')
DATE_RU = re.compile(r'(?<!\d)([0-3]?\d)[./]([01]?\d)[./]((?:20|21)\d{2})(?!\d)')


def strip_wikitext(raw):
    """Remove navigation/markup while preserving meaningful infobox fields."""
    raw = re.sub(
        r'<(?:style|script|noinclude|includeonly)[^>]*>.*?</(?:style|script|noinclude|includeonly)>',
        ' ', raw, flags=re.I | re.S)
    raw = re.sub(r'<!--.*?-->', ' ', raw, flags=re.S)
    fields = []
    for field in re.finditer(r'^\|\s*([а-яА-Яa-zA-Z_]{2,30})\s*=\s*(.+)$', raw, flags=re.M):
        key, value = field.group(1).strip(), field.group(2).strip()
        if key.startswith(('флаг','схема','изображени','портрет','карта','навигац','логотип','шаблон','архив')):
            continue
        if len(value) > 200:
            continue
        fields.append(f'{key.replace("_", " ")}: {value}')

    chars = []
    depth = 0
    i = 0
    while i < len(raw):
        if raw.startswith('{{', i):
            depth += 1
            i += 2
            continue
        if raw.startswith('}}', i) and depth:
            depth -= 1
            i += 2
            continue
        if not depth:
            chars.append(raw[i])
        i += 1
    raw = ''.join(chars)
    raw = re.sub(r'\[\[(?:Файл|Изображение|Категория):[^\]]*\]\]', '', raw, flags=re.I)
    raw = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', raw)
    raw = re.sub(r'\[\[([^\]]+)\]\]', r'\1', raw)
    raw = re.sub(r'\[https?://[^\s\]]+\s+([^\]]+)\]', r'\1', raw)
    raw = re.sub(r'\[https?://[^\]]+\]', '', raw)
    raw = re.sub(r'<ref[^>]*>.*?</ref>', '', raw, flags=re.I | re.S)
    raw = re.sub(r'<[^>]+>', ' ', raw)
    raw = html.unescape(raw)
    raw = re.sub(r"'{2,5}", '', raw)
    raw = re.sub(r'^={2,6}\s*(.*?)\s*={2,6}\s*$', r'Раздел: \1', raw, flags=re.M)
    raw = re.sub(r'^\{\|.*?$|^\|\}.*?$|^\|-.*?$|^\|\+.*?$|^\|\s*colspan.*?$|^\|\s*rowspan.*?$',
                 '', raw, flags=re.M)
    raw = re.sub(r'^\s*[!|]\s*', '', raw, flags=re.M)
    raw = re.sub(r'\b__\w+__\b', '', raw)
    raw = re.sub(r'[ \t]{2,}', ' ', raw)
    raw = re.sub(r'\n[ \t]*\n[ \t]*\n+', '\n\n', raw).strip()
    clean_fields = []
    for field in fields:
        field = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', field)
        field = re.sub(r'\[\[([^\]]+)\]\]', r'\1', field)
        field = re.sub(r'\{\{.*?\}\}', '', field)
        if not EXTERNAL_NAMES.search(field):
            clean_fields.append(field)
    return '\n'.join(clean_fields) + '\n\n' + raw if clean_fields else raw


def fragments(title, text, length=1050):
    """Return compact passages with the article title on every passage."""
    section = ''
    chunk = ''
    for paragraph in re.split(r'\n{2,}', text):
        para = paragraph.strip()
        if not para:
            continue
        if para.startswith('Раздел: '):
            section = para[8:90]
        if len(para) > length:
            pieces = []
            for line in re.split(r'(?<=[.!?])\s+(?=[А-ЯA-Z])|\n(?=[*\-])|\n(?=\w)', para):
                if len(line) > length:
                    pieces.extend(line[i:i+length] for i in range(0, len(line), length))
                else:
                    pieces.append(line)
        else:
            pieces = [para]
        for piece in pieces:
            prefix = f'{title}. ' + (f'{section}. ' if section and section != title else '')
            if len(prefix) + len(piece) > length + 150:
                piece = piece[:length]
            if chunk and (len(chunk) + len(piece) + 2 > length or event_date(chunk) != event_date(piece)):
                yield title + '. ' + chunk
                chunk = ''
            chunk += ('\n' if chunk else '') + piece
    if chunk:
        yield title + '. ' + chunk


def event_date(fragment, public_from='2061-11-12'):
    """Never expose future 2062+ lore in the 2061 context."""
    years = [int(x) for x in YEAR.findall(fragment)]
    if years and max(years) > 2061:
        return f'{max(years):04}-12-31'
    if years and max(years) == 2061:
        dates = []
        for match in DATE_ISO.finditer(fragment):
            y, month, day = map(int, match.groups())
            if y == 2061 and 1 <= month <= 12 and 1 <= day <= 31:
                dates.append(f'{y:04}-{month:02}-{day:02}')
        for match in DATE_RU.finditer(fragment):
            day, month, y = map(int, match.groups())
            if y == 2061 and 1 <= month <= 12 and 1 <= day <= 31:
                dates.append(f'{y:04}-{month:02}-{day:02}')
        return max(dates) if dates else public_from
    return '0001-01-01'


def wiki_entries(xml_file, source='Aurelia Wiki, экспорт 2026-11-12'):
    root = ET.parse(xml_file).getroot()
    for page in root.findall('{*}page'):
        title = page.findtext('{*}title', '').strip()
        namespace = page.findtext('{*}ns', '0')
        if not title or namespace != '0' or title.startswith(SKIP_PREFIX):
            continue
        revision = page.find('.//{*}revision/{*}text')
        raw = (revision.text or '') if revision is not None else ''
        if not raw.strip() or re.match(r'^\s*#REDIRECT', raw, re.I):
            continue
        for idx, piece in enumerate(fragments(title, strip_wikitext(raw))):
            if len(piece) < 50 or EXTERNAL_NAMES.search(piece):
                continue
            # A stale figure in the Wiki must not override a newer author fact.
            if title == 'Кефирстан' and '2 320 032' in piece:
                piece = piece.replace('2 320 032', '[прежние данные о площади]')
            day = event_date(piece)
            digest = hashlib.sha256((title + '#' + str(idx)).encode()).hexdigest()[:24]
            yield {'id': 'wiki-20261112-' + digest,
                   'source': f'{source}: {title}', 'text': piece,
                   'event_date': day, 'public_from': '2061-11-12',
                   'access': 'public', 'status': 'approved',
                   'kind': 'canon', 'revision': 1}


def load_wiki(db, paths):
    """Import a multipart, read-only Wiki snapshot into existing SQLite/FTS."""
    from .knowledge import put
    payload = '\n'.join(Path(part).read_text(encoding='utf-8') for part in paths)
    inserted = 0
    with db.transaction() as conn:
        for entry in wiki_entries(StringIO(payload)):
            put(db, **entry, connection=conn)
            inserted += 1
    return inserted
