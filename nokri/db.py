import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager

SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, role TEXT DEFAULT 'user', blocked INTEGER DEFAULT 0,
 filter_on INTEGER DEFAULT 1, unlimited_until REAL DEFAULT 0, model TEXT DEFAULT 'flash', language TEXT DEFAULT 'ru');
CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY, config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets(provider TEXT PRIMARY KEY,cipher TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS limits(uid INTEGER,model TEXT,h3 INTEGER NOT NULL,day INTEGER NOT NULL,allowed INTEGER DEFAULT 1,PRIMARY KEY(uid,model));
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,update_id TEXT UNIQUE,uid INTEGER,model TEXT,at REAL,status TEXT,
 input_tokens INTEGER,output_tokens INTEGER,cost REAL,error_code TEXT);
CREATE INDEX IF NOT EXISTS requests_window ON requests(uid,model,at);
CREATE TABLE IF NOT EXISTS budgets(uid INTEGER PRIMARY KEY,h3_nusd INTEGER NOT NULL CHECK(h3_nusd>=0),day_nusd INTEGER NOT NULL CHECK(day_nusd>=0));
CREATE TABLE IF NOT EXISTS spending(update_id TEXT PRIMARY KEY,uid INTEGER NOT NULL,at REAL NOT NULL,state TEXT NOT NULL,
 amount_nusd INTEGER,reserved_nusd INTEGER NOT NULL,input_rate TEXT NOT NULL,output_rate TEXT NOT NULL,settled_at REAL);
CREATE INDEX IF NOT EXISTS spending_window ON spending(uid,state,at);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,uid INTEGER,model TEXT,role TEXT,payload TEXT,at REAL,day TEXT);
CREATE INDEX IF NOT EXISTS messages_user ON messages(uid,id);
CREATE TABLE IF NOT EXISTS knowledge(id TEXT PRIMARY KEY,source TEXT,text TEXT,event_date TEXT,public_from TEXT,
 access TEXT DEFAULT 'restricted',status TEXT DEFAULT 'draft',kind TEXT DEFAULT 'canon',revision INTEGER DEFAULT 1);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(id UNINDEXED,text,tokenize='unicode61');
CREATE TABLE IF NOT EXISTS objects(id INTEGER PRIMARY KEY,name TEXT UNIQUE,description TEXT,created_day TEXT,status TEXT DEFAULT 'generated');
CREATE TABLE IF NOT EXISTS api_usage(update_id TEXT PRIMARY KEY,usage_json TEXT,reported_model TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor INTEGER,action TEXT,target TEXT,at REAL);
'''

class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self.connect() as c:
            c.executescript(SCHEMA)

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    @contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.transaction() as c:
            c.execute(sql, args)

    def user(self, uid):
        self.execute('INSERT OR IGNORE INTO users(id) VALUES(?)', (uid,))
        return self.rows('SELECT * FROM users WHERE id=?', (uid,))[0]

    def setting(self, key, default=None):
        r = self.rows('SELECT value FROM settings WHERE key=?', (key,))
        return json.loads(r[0]['value']) if r else default

    def set(self, key, value):
        self.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key,json.dumps(value)))

    def model(self, key):
        r = self.rows('SELECT config FROM models WHERE id=?', (key,))
        if not r:
            raise ValueError('Неизвестная модель')
        return json.loads(r[0]['config'])

    def put_model(self, key, config):
        self.execute('INSERT OR REPLACE INTO models VALUES(?,?)',(key,json.dumps(config,ensure_ascii=False)))
