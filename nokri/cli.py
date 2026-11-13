import argparse
import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from .db import DB
from .provider import Vault,Provider
from .service import Service
from .knowledge import load_curated,import_tree
from .budget import Budgets, usd_to_nusd
import time

def initialize(db):
    Budgets(db,time.time).migrate_legacy()
    if db.setting('default_budget') is None:
        budget=json.loads(Path('config/budgets.json').read_text())
        db.set('default_budget',{'h3_nusd':usd_to_nusd(budget['default_h3']),'day_nusd':usd_to_nusd(budget['default_day'])})
    for k,v in json.loads(Path('config/models.json').read_text()).items():
        if not db.rows('SELECT 1 FROM models WHERE id=?',(k,)): db.put_model(k,v)
        old=db.model(k)
        if 'h3' in old or 'day' in old:
            old.pop('h3',None);old.pop('day',None);db.put_model(k,old)
    if not db.setting('seed_loaded',False):
        load_curated(db,'data/curated.jsonl.gz'); db.set('seed_loaded',True)

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['run','init','import','load','backup','doctor'])
    parser.add_argument('path',nargs='?')
    args=parser.parse_args()
    db=DB(os.getenv('DATABASE_PATH','state/nokri.sqlite'))
    initialize(db)
    if not os.getenv('OWNER_ID'):
        os.environ['OWNER_ID']=str(json.loads(Path('config/deployment.json').read_text())['owner_id'])
    if args.command=='init': print('База создана. Заполните окружение, затем nokri run'); return
    if args.command=='import':
        if not args.path: parser.error('Укажите каталог распакованного архива')
        report=import_tree(db,args.path)
        Path('state').mkdir(exist_ok=True)
        Path('state/import_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print('Импортировано в карантин. Отчет state/import_report.json'); return
    if args.command=='load':
        if not args.path: parser.error('Укажите JSONL с проверенными метаданными')
        load_curated(db,args.path); print('Загружено'); return
    if args.command=='backup':
        if not args.path: parser.error('Укажите путь резервной копии')
        with db.connect() as src,sqlite3.connect(args.path) as dest: src.backup(dest)
        print('Резервная копия создана. Храните MASTER_KEY отдельно'); return
    if args.command=='doctor':
        print(json.dumps({'database':db.rows('PRAGMA integrity_check'),'models':len(db.rows('SELECT id FROM models')),
                          'knowledge':db.rows('SELECT status,access,count(*) n FROM knowledge GROUP BY status,access'),
                          'owner_configured':os.getenv('OWNER_ID','').isdigit(),
                          'owner_id':int(os.environ['OWNER_ID']),
                          'default_budget':db.setting('default_budget'),
                          'telegram_configured':bool(os.getenv('TELEGRAM_BOT_TOKEN')),
                          'master_configured':bool(os.getenv('MASTER_KEY'))},ensure_ascii=False,indent=2)); return
    for env in ('TELEGRAM_BOT_TOKEN','OWNER_ID','MASTER_KEY'):
        if not os.getenv(env): parser.error('Задайте '+env+' в окружении. См. README.md')
    owner=int(os.environ['OWNER_ID'])
    if owner<=0: parser.error('OWNER_ID должен быть положительным числовым ID владельца')
    vault=Vault(db,os.environ['MASTER_KEY'])
    # Explicit env key is an optional bootstrap; later Telegram rotations take priority.
    if os.getenv('ANYMODEL_API_KEY') and not db.rows('SELECT 1 FROM secrets WHERE provider=?',('anymodel',)):
        vault.put('anymodel',os.environ['ANYMODEL_API_KEY'])
    # Unknown in-flight billing on restart: keep monetary reservation, do not retry automatically.
    db.execute("UPDATE requests SET status='interrupted',error_code='restart_unknown_billing' WHERE status='pending'")
    from .telegram import Telegram
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    service=Service(db,Provider(vault),owner)
    asyncio.run(Telegram(os.environ['TELEGRAM_BOT_TOKEN'],service,vault).run())

if __name__=='__main__': main()
