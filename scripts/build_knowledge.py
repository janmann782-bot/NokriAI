"""Reproducible language import and conservative archive index. Run from project root."""
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nokri.db import DB
from nokri.knowledge import import_tree,put,load_curated

root=Path(sys.argv[1]); out=Path('assets'); out.mkdir(exist_ok=True)
db=DB('state/seed.sqlite')
report=import_tree(db,root)
(out/'archive_manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
entries=[]
for folder,kind in [('Сильмир','silmir'),('Новосильмир','novosilmir')]:
 for p in (root/'языки Аурелии'/folder).rglob('*.md'):
  for i,line in enumerate(p.read_text().splitlines()):
   if not re.match(r'^[^\s#|].{0,90} [—–-] .+',line): continue
   entries.append(dict(id=hashlib.sha256((str(p.relative_to(root))+str(i)).encode()).hexdigest()[:24],
     source=str(p.relative_to(root)),text=line,event_date='0001-01-01',public_from='0001-01-01',
     access='public',status='approved',kind=kind))
# A few explicitly established facts, not the archive's conflicting statistics.
facts=[
 ('identity','Nokri AI - государственный ИИ Союза Автономных Республик Кефирстан, САР Кефирстан. Сокращение вооруженных сил - ВС САРК.','2050-01-01','Требования владельца'),
 ('capital','Каменный Мост - столица Кефирстана и Кефирии. В Кефирстане есть города Твороград, Молозорь, Кефирск, Ниженовогород.','2050-01-01','Требования владельца / Вики/Статьи/Каменный Мост.wiki'),
 ('world','Аурелия - планета Фарфарийской системы. Основные континенты - Эниварра, Саргатия и Феридия (Ферридия). Естественный спутник - Силень.','0001-01-01','Вики/Статьи/Аурелия.wiki, географические сведения'),
 ('phone','Утвержденный телефонный код Кефирстана +201. Зоны: +1xx западная Эниварра, +2xx Кефирстан и страны восточнее, +3xx Саргатия, +4xx Ферридия. Остальные национальные и региональные коды пока предварительные.','2061-11-13','Требования владельца'),
 ('ralizm','Тимофей Рализм - президент Кефирстана с 2049 года. Партия - ЛПК.','2049-12-31','Требования владельца / Вики/Статьи/Кефирстан.wiki'),
 ('vor','Вордсфалия имеет территории в Эниварре и Саргатии. Однозначно относить всю страну к западной Эниварре нельзя.','0001-01-01','Вики/Статьи/Вордсфалия.wiki, Карты/Политическая 2061.png'),
 ('nidr','Нидросия (Республика Ниабавия) находится на континенте Феридия.','0001-01-01','Вики/Статьи/Нидросия.wiki'),
 ('med','Медия, Ноланская Федерация и Саргатийская Федерация находятся в Саргатии.','0001-01-01','Вики/Статьи/Медия.wiki, политическая карта'),
 ('geo','Кефирстан находится в Эниварре. Дервесния расположена западнее Кефирстана, Веранд восточнее.','0001-01-01','Карты/Политическая 2061.png'),
]
for id,text,day,source in facts:
 entries.append(dict(id='seed-'+id,source=source,text=text,event_date=day,public_from=day,access='public',status='approved',kind='canon'))
(out/'curated.jsonl').write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in entries)+'\n')
load_curated(db,out/'curated.jsonl')
db.set('seed_loaded',True)
shutil.copyfile(root/'языки Аурелии/Новосильмир/ПРАВИЛА НОВОСИЛЬМИРА.txt',out/'novosilmir_grammar.txt')
for r in db.rows("SELECT id,config FROM models"): pass
print(json.dumps({'files':len(report),'errors':[r for r in report if r['status']=='error'],
 'curated':len(entries),'knowledge':db.rows('SELECT kind,status,count(*) n FROM knowledge GROUP BY kind,status')},ensure_ascii=False))
