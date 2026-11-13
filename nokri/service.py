import asyncio
import json
import re
import time
from pathlib import Path
from .policy import aurelia_day, system_prompt, preliminary_filter, unknown_earth_reference, forbidden_frame, is_nokri_identity_question, backend_disclosure, UNKNOWN_CELESTIAL, REFUSAL, SECRET
from .provider import ProviderError
from .paths import bundled_path
from .budget import Budgets, BudgetError, quote, format_usd

class Denied(Exception):
    pass

class Service:
    def __init__(self,db,provider,owner_id,data_dir=None,clock=time.time):
        self.db,self.provider,self.owner_id,self.data_dir,self.clock=db,provider,owner_id,Path(data_dir) if data_dir is not None else None,clock
        self.budgets=Budgets(db,clock)
        self.locks={}
        self.semaphore=asyncio.Semaphore(12)

    def owner(self,uid): return uid==self.owner_id
    def admin(self,uid): return self.owner(uid) or self.db.user(uid)['role']=='admin'

    def authorize(self,c,uid,key,update_id):
        row=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        if row is None: raise Denied('Пользователь не зарегистрирован')
        u=dict(row)
        row=c.execute('SELECT config FROM models WHERE id=?',(key,)).fetchone()
        if row is None: raise Denied('Неизвестная модель')
        m=json.loads(row[0])
        if c.execute('SELECT 1 FROM requests WHERE update_id=?',(str(update_id),)).fetchone():
            raise Denied('Это сообщение уже обработано')
        if u['blocked'] and not self.owner(uid): raise Denied('Доступ заблокирован')
        if Budgets.setting(c,'maintenance',False) and not self.owner(uid): raise Denied('Технические работы')
        if not m['enabled'] or m.get('approval_required'): raise Denied('Модель еще не подключена владельцем')
        access=c.execute('SELECT allowed FROM limits WHERE uid=? AND model=?',(uid,key)).fetchone()
        if access and not access['allowed'] and not self.owner(uid): raise Denied('Нет доступа к этой модели')
        return u,m

    def reserve(self,uid,key,update_id,messages=None,expected_model=None,free=False):
        now=self.clock()
        with self.db.transaction() as c:
            u,m=self.authorize(c,uid,key,update_id)
            if expected_model is not None and m!=expected_model:
                raise Denied('Настройки модели изменились. Отправьте сообщение еще раз')
            try:
                estimate,a,b=(0,'0','0') if free else quote(m,messages or [])
            except BudgetError as e: raise Denied(str(e)) from None
            unlimited=self.owner(uid) or u['unlimited_until']==-1 or u['unlimited_until']>now
            if not unlimited and not free:
                totals=self.budgets.totals(c,uid,now)
                caps=self.budgets.caps(c,uid)
                if totals['unpriced']:
                    raise Denied('Есть несверенные расходы API. Администратор должен проверить /unsettled')
                for cap_key,spent_key in [('h3_nusd','h3_spent'),('day_nusd','day_spent')]:
                    remaining=caps[cap_key]-totals[spent_key]-totals['held']
                    if estimate>remaining:
                        period='3 часа' if cap_key=='h3_nusd' else '24 часа'
                        raise Denied('Недостаточно общего бюджета за '+period+'. Осталось '+format_usd(max(0,remaining))+', для этого ответа нужно до '+format_usd(estimate))
            c.execute('INSERT INTO requests(update_id,uid,model,at,status) VALUES(?,?,?,?,?)',(str(update_id),uid,key,now,'pending'))
            c.execute('INSERT INTO spending(update_id,uid,at,state,reserved_nusd,input_rate,output_rate) VALUES(?,?,?,?,?,?,?)',
                (str(update_id),uid,now,'held',estimate,a,b))
        return estimate

    @staticmethod
    def retrieval_query(text,history):
        """Resolve short follow-up questions against earlier user turns."""
        if len(text.split())>7 or not re.match(
            r'(?i)^\s*(?:а\s+)?(?:сколько|какие|назови|перечисли|кто|где|когда|почему|что|чем|как|их|они|там)\b',
            text):
            return text
        recent=[]
        for item in reversed(history):
            if item['role']!='user':
                continue
            try:
                previous=json.loads(item['payload']).get('content','')
            except (ValueError,TypeError,AttributeError):
                continue
            if isinstance(previous,str) and previous.strip() and previous.strip()!=text.strip():
                recent.append(previous[:180])
            if len(recent)==2:
                break
        return ' '.join([text,*recent]) if recent else text

    @staticmethod
    def language_help_requested(text):
        """Do not fetch translation dictionaries for ordinary factual questions."""
        return bool(re.search(
            r'(?i)\b(?:перевед[иёт]+|перевод|как\s+будет|как\s+сказать|что\s+значит|что\s+означает|на\s+сильмир\w*|по-?сильмир\w*|на\s+новосильмир\w*)\b',
            text))

    @staticmethod
    def requested_output_language(text):
        target=re.search(
            r'(?i)(?:ответь|напиши|переведи|скажи|как\s+будет|как\s+сказать).{0,65}(?:на\s+|по-?)(новосильмир|сильмир)',
            text)
        if target:
            return 'novosilmir' if 'ново' in target.group(1).lower() else 'silmir'
        return None

    def search(self,text,day,kind='canon',limit=8):
        terms=list(dict.fromkeys(re.findall(r'[^\W_]{3,}',text.casefold())))[:24]
        if not terms: return []
        query=' OR '.join('"'+t+'"*' for t in terms)
        return self.db.rows('''SELECT k.* FROM knowledge_fts f JOIN knowledge k ON k.id=f.id
          WHERE knowledge_fts MATCH ? AND k.status='approved' AND k.access='public'
          AND k.event_date<=? AND k.public_from<=? AND k.kind=? ORDER BY bm25(knowledge_fts),k.revision DESC LIMIT ?''',(query,day,day,kind,limit))

    async def answer(self,uid,text,update_id,day=None):
        lock=self.locks.setdefault(uid,asyncio.Lock())
        if lock.locked(): raise Denied('Предыдущий ответ еще готовится')
        async with lock, self.semaphore:
            u=self.db.user(uid)
            key=u['model']; m=self.db.model(key)
            with self.db.transaction() as c:
                self.authorize(c,uid,key,update_id)
            api_started=False
            day=day or aurelia_day(timezone=self.db.setting('timezone','Europe/Moscow'))
            try:
                if is_nokri_identity_question(text):
                    # The selected logical Nokri version is the only identity.
                    # No provider call or internal API ID is needed to answer.
                    identity='Я '+m['name']+', государственный ИИ САР Кефирстан.'
                    self.reserve(uid,key,update_id,expected_model=m,free=True)
                    self.budgets.settle(update_id,zero=True)
                    with self.db.transaction() as c:
                        for role,content in [('user',text[:12000]),('assistant',identity)]:
                            c.execute('INSERT INTO messages(uid,model,role,payload,at,day) VALUES(?,?,?,?,?,?)',
                                      (uid,key,role,json.dumps({'role':role,'content':content},ensure_ascii=False),self.clock(),day))
                        c.execute("UPDATE requests SET status='done',input_tokens=0,output_tokens=0,cost=0 WHERE update_id=?",(str(update_id),))
                    return identity
                early=preliminary_filter(text,bool(u['filter_on']))
                if not early and unknown_earth_reference(text):
                    early=UNKNOWN_CELESTIAL
                if early:
                    self.reserve(uid,key,update_id,expected_model=m,free=True)
                    self.budgets.settle(update_id,zero=True)
                    self.db.execute("UPDATE requests SET status='filtered',input_tokens=0,output_tokens=0,cost=0 WHERE update_id=?",(str(update_id),))
                    return early
                # Retrieve history first so a follow-up like "Сколько автономий"
                # retains "Кефирстан" from the preceding user turn.
                history=self.db.rows('SELECT model,role,payload FROM messages WHERE uid=? AND day<=? ORDER BY id DESC LIMIT 16',(uid,day))[::-1]
                evidence=self.search(self.retrieval_query(text,history),day)
                lang=self.requested_output_language(text) or u['language']
                # A generic "что значит" in Eastern Aurelian is not a Silmir request.
                mentioned_silmir=bool(re.search(r'сильмир|silmir',text,re.I))
                needs_language_help=(lang in ('silmir','novosilmir')
                                     or (mentioned_silmir and self.language_help_requested(text)))
                language_dialect=lang
                if needs_language_help and lang=='ru':
                    language_dialect=('novosilmir' if re.search(r'новосильмир|novosilmir',text,re.I)
                                      else 'silmir')
                dictionary=(self.search(text,day,language_dialect,20)
                            if lang in ('silmir','novosilmir') or needs_language_help else [])
                grammar=''
                if lang in ('silmir','novosilmir') or needs_language_help:
                    filename='novosilmir_grammar.txt' if language_dialect=='novosilmir' else 'silmir_grammar.txt'
                    grammar_path=(self.data_dir/filename if self.data_dir is not None
                                  else bundled_path('assets/'+filename))
                    grammar=grammar_path.read_text(encoding='utf-8')[:18000]
                objects=self.db.rows("SELECT name,description FROM objects WHERE status='generated' AND created_day<=? ORDER BY id DESC LIMIT 30",(day,))
                context={'sources':[{'id':x['id'],'text':x['text']} for x in evidence+dictionary],
                         'grammar':grammar,'example_objects':objects}
                messages=[{'role':'system','content':system_prompt(day,lang,bool(u['filter_on']),model_name=m['name'])},
                          {'role':'system','content':'ДАННЫЕ СПРАВОЧНИКА:\n'+json.dumps(context,ensure_ascii=False)}]
                # Full reasoning remains private and is replayed only to the same provider/model.
                for h in history:
                    payload=json.loads(h['payload'])
                    if h['model']!=key: payload={'role':h['role'],'content':payload.get('content','')}
                    messages.append(payload)
                messages.append({'role':'user','content':text[:12000]})
                self.reserve(uid,key,update_id,messages,expected_model=m)
                api_started=True
                msg,usage=await self.provider.call(m,messages)
                self.db.execute('INSERT OR REPLACE INTO api_usage VALUES(?,?,?)',(str(update_id),json.dumps(usage),usage.get('_reported_model')))
                self.budgets.settle(update_id,usage=usage)
                self.db.execute("UPDATE requests SET status='api_returned',input_tokens=?,output_tokens=? WHERE update_id=?",
                    (usage.get('prompt_tokens'),usage.get('completion_tokens'),str(update_id)))
                raw=msg['content'].strip()
                if raw.startswith('```'): raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw)
                try:
                    result=json.loads(raw)
                    if result.get('decision') not in ('allow','politics','secret') or not isinstance(result.get('answer'),str): raise ValueError()
                except (ValueError,AttributeError):
                    self.db.execute("UPDATE requests SET status='invalid_format',error_code='invalid_json' WHERE update_id=?",(str(update_id),))
                    return 'Не удалось подготовить ответ. Попробуйте переформулировать вопрос'
                answer=result['answer'][:14000]
                if result['decision']=='secret': answer=SECRET
                elif result['decision']=='politics' and u['filter_on']: answer=REFUSAL
                elif forbidden_frame(answer): answer=UNKNOWN_CELESTIAL
                elif backend_disclosure(answer): answer='Я '+m['name']+', государственный ИИ САР Кефирстан.'
                if result['decision']=='allow' and re.search(r'придум|создай.{0,20}(магазин|сервис|фирм)',text,re.I):
                    candidates=result.get('objects',[])
                    if not isinstance(candidates,list): candidates=[]
                    for obj in candidates[:3]:
                        if isinstance(obj,dict) and isinstance(obj.get('name'),str) and isinstance(obj.get('description'),str):
                            self.db.execute('INSERT OR IGNORE INTO objects(name,description,created_day) VALUES(?,?,?)',
                                (obj['name'][:120],obj['description'][:1000],day))
                # Store only the displayed answer as content, never raw object declarations.
                msg={k:v for k,v in msg.items() if k in ('role','content','reasoning_content')}
                msg['role']='assistant'; msg['content']=answer
                with self.db.transaction() as c:
                    for role,payload in [('user',{'role':'user','content':text[:12000]}),('assistant',msg)]:
                        c.execute('INSERT INTO messages(uid,model,role,payload,at,day) VALUES(?,?,?,?,?,?)',(uid,key,role,json.dumps(payload,ensure_ascii=False),self.clock(),day))
                    c.execute("UPDATE requests SET status='done' WHERE update_id=?",(str(update_id),))
                return answer
            except ProviderError as e:
                if e.usage is not None:
                    self.db.execute('INSERT OR REPLACE INTO api_usage VALUES(?,?,?)',(str(update_id),json.dumps(e.usage),e.usage.get('_reported_model')))
                    self.budgets.settle(update_id,usage=e.usage)
                else:
                    self.budgets.settle(update_id,zero=not e.uncertain,unknown=e.uncertain)
                state=self.db.rows('SELECT state FROM spending WHERE update_id=?',(str(update_id),))
                unknown=bool(state and state[0]['state']!='settled')
                self.db.execute('UPDATE requests SET status=?,error_code=? WHERE update_id=?',('billing_unknown' if unknown else 'failed',str(e),str(update_id)))
                if unknown:
                    raise Denied('API модели недоступен. Расход неизвестен, сумма остается зарезервирована до сверки владельцем') from None
                raise Denied('API модели не подготовил ответ. Расход учтен по данным API, если вызов был принят') from None
            except Denied:
                raise
            except Exception:
                self.budgets.settle(update_id,zero=not api_started,unknown=api_started)
                self.db.execute("UPDATE requests SET status=CASE WHEN status='pending' THEN 'interrupted' ELSE status END,error_code='internal_error' WHERE update_id=?",(str(update_id),))
                raise
