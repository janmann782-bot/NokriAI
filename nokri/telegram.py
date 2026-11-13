import asyncio
from contextlib import suppress
import json
import logging
import time
import httpx
from .service import Denied
from .provider import ProviderError, validate_model
from .policy import aurelia_day
from .budget import BudgetError, usd_to_nusd, format_usd, decimal, SCALE, DEFAULT_BUDGET, rates

log=logging.getLogger('nokri')
ADMIN_HELP='''Панель управления Nokri
/users - последние пользователи
/user ID - карточка
/budget ID USD_3H USD_24H - личный общий бюджет
/budget ID default - вернуть бюджет по умолчанию
/budget_default USD_3H USD_24H - бюджет по умолчанию (владелец)
/price MODEL INPUT OUTPUT - USD за 1M токенов (владелец)
/access ID MODEL 0|1 - доступ к модели
/unlimited ID forever|off|ЧАСЫ
/reset ID - сброс потраченного бюджета, все модели
/unsettled - расходы, требующие сверки
/settle REQUEST_ID USD - сверить неизвестный расход (владелец)
/ban ID 0|1
/filter ID 0|1
/usage - токены и расчет расходов
/kb ПОИСК - архив для проверки (админ)
/inspect KB_ID - полный фрагмент и метаданные
Владелец:
/key - смена общего ключа AnyModel
/catalog - список разрешенных китайских моделей API
/modelconfig MODEL - настройки JSON
/setmodel MODEL JSON - смена соответствия/тарифа/названия
/map MODEL API_ID - быстрая смена соответствия
/enable MODEL 0|1 - включить (также согласует предложенную замену)
/adminrole ID 0|1 - назначить администратора
/global maintenance true|false
/global timezone Europe/Moscow
/review KB_ID YYYY-MM-DD YYYY-MM-DD public|restricted|secret approved|draft
/objects - реестр бытовых примеров
/object_delete ID - удалить пример
/cancel - отмена ввода ключа'''

class Telegram:
    def __init__(self,token,service,vault,transport=None):
        self.url='https://api.telegram.org/bot'+token
        self.s,self.db,self.vault=service,service.db,vault
        self.http=httpx.AsyncClient(timeout=65,transport=transport,follow_redirects=False)
        self.pending={}

    async def api(self,method,**kwargs):
        r=await self.http.post(self.url+'/'+method,json=kwargs)
        if r.status_code!=200: raise RuntimeError('telegram_http_'+str(r.status_code))
        body=r.json()
        if not body.get('ok'): raise RuntimeError('telegram_api_error')
        return body['result']

    async def send(self,chat,text,keyboard=None):
        # UTF-16 safe: at most 1800 code points per Telegram message.
        for i in range(0,len(text) or 1,1800):
            kw={'chat_id':chat,'text':text[i:i+1800] or 'Нет данных'}
            if i==0 and keyboard: kw['reply_markup']={'inline_keyboard':keyboard}
            await self.api('sendMessage',**kw)

    async def typing(self,chat):
        while True:
            try: await self.api('sendChatAction',chat_id=chat,action='typing')
            except Exception: pass
            await asyncio.sleep(4)

    async def animated_answer(self,chat,text,uid,pause=asyncio.sleep):
        if not self.db.setting('animation:'+str(uid),True) or len(text)<35:
            await self.send(chat,text); return
        first=text[:1800]
        sent=await self.api('sendMessage',chat_id=chat,text=first[:22]+' ▍')
        message_id=sent['message_id']
        try:
            for end in (60,140,300,700,1200):
                if end>=len(first): break
                await pause(1.1)
                await self.api('editMessageText',chat_id=chat,message_id=message_id,text=first[:end]+' ▍')
            await pause(1.1)
            await self.api('editMessageText',chat_id=chat,message_id=message_id,text=first)
        except Exception:
            # A failed edit must never strand the answer as a partial prefix.
            await self.send(chat,text)
            return
        if len(text)>1800: await self.send(chat,text[1800:])

    def audit(self,uid,action,target=''):
        self.db.execute('INSERT INTO audit(actor,action,target,at) VALUES(?,?,?,?)',(uid,action,str(target),time.time()))

    async def handle(self,update):
        cb=update.get('callback_query')
        if cb:
            uid=cb['from']['id']; msg=cb.get('message',{})
            if msg.get('chat',{}).get('type')!='private' or msg['chat']['id']!=uid: return
            await self.api('answerCallbackQuery',callback_query_id=cb['id'])
            action=cb.get('data','')
            if action.startswith('model:'): text='/model '+action.split(':',1)[1]
            elif action in ('models','limits','new','admin','key','usage','users'): text='/'+action
            else: return
        else:
            msg=update.get('message',{}); uid=msg.get('from',{}).get('id')
            if uid is None or msg.get('chat',{}).get('type')!='private': return
            text=msg.get('text','')
        chat=msg['chat']['id']; u=self.db.user(uid)
        if u['blocked'] and not self.s.owner(uid):
            await self.send(chat,'Доступ заблокирован'); return
        try:
            if text=='/cancel':
                self.pending.pop(uid,None); await self.send(chat,'Ввод отменен'); return
            if uid in self.pending:
                expires=self.pending.pop(uid)
                if expires<time.time(): raise Denied('Время ввода ключа истекло. Нажмите /key')
                if not self.s.owner(uid): raise Denied('Только владелец')
                # Never send text to a model or persist it in message history.
                if not cb:
                    try: await self.api('deleteMessage',chat_id=chat,message_id=msg['message_id'])
                    except Exception:
                        await self.send(chat,'Telegram не дал удалить сообщение с ключом. Удалите его вручную')
                self.vault.put('anymodel',text.strip())
                self.audit(uid,'key_rotated','anymodel')
                await self.send(chat,'Общий ключ AnyModel заменен. Следующие запросы всех пользователей используют его'); return
            if text.startswith('/'):
                await self.command(uid,chat,text); return
            if not text: return
            pulse=asyncio.create_task(self.typing(chat))
            try:
                answer=await self.s.answer(uid,text,update['update_id'])
            finally:
                pulse.cancel()
                with suppress(asyncio.CancelledError): await pulse
            await self.animated_answer(chat,answer,uid)
        except (Denied,ValueError,KeyError,IndexError) as e:
            await self.send(chat,str(e) if isinstance(e,(Denied,ValueError)) else 'Неверные аргументы. Откройте /admin или /help')
        except Exception:
            # Do not log exception bodies/URLs: SDKs may include token or prompt.
            log.error('handler_failed uid=%s update=%s',uid,update.get('update_id'))
            await self.send(chat,'Ошибка обработки. Код: internal_error')

    async def command(self,uid,chat,text):
        args=text.split(); cmd=args[0].split('@')[0]; s=self.s
        if cmd in ('/start','/help'):
            await self.send(chat,'Nokri AI · САР Кефирстан\nВыберите модель. Общий бюджет на все модели. Расход зависит от модели и длины ответа\n/model · /limits · /language ru|silmir|novosilmir · /new · /myid',
                [[{'text':'Модели','callback_data':'models'},{'text':'Мой бюджет','callback_data':'limits'}],[{'text':'Новый диалог','callback_data':'new'}]]); return
        if cmd=='/animation':
            if len(args)!=2 or args[1] not in ('on','off'): raise ValueError('/animation on или /animation off')
            self.db.set('animation:'+str(uid),args[1]=='on'); await self.send(chat,'Анимация '+('включена' if args[1]=='on' else 'выключена')); return
        if cmd=='/myid': await self.send(chat,str(uid)); return
        if cmd in ('/models','/model'):
            if len(args)>1:
                m=self.db.model(args[1]); access=self.db.rows('SELECT allowed FROM limits WHERE uid=? AND model=?',(uid,args[1]))
                if not m['enabled'] or m.get('approval_required') or (access and not access[0]['allowed'] and not s.owner(uid)): raise Denied('Модель недоступна')
                self.db.execute('UPDATE users SET model=? WHERE id=?',(args[1],uid))
                await self.send(chat,'Выбрана '+m['name']); return
            allm=[(x['id'],json.loads(x['config'])) for x in self.db.rows('SELECT * FROM models')]
            allm.sort(key=lambda x:x[1]['rank'])
            await self.send(chat,'Модели Nokri',[[{'text':m['name']+(' · выключена' if not m['enabled'] else ''),'callback_data':'model:'+k}] for k,m in allm]); return
        if cmd=='/language':
            if args[1] not in ('ru','silmir','novosilmir'): raise ValueError('ru, silmir или novosilmir')
            self.db.execute('UPDATE users SET language=? WHERE id=?',(args[1],uid)); await self.send(chat,'Язык сохранен'); return
        if cmd=='/new':
            self.db.execute('DELETE FROM messages WHERE uid=?',(uid,)); await self.send(chat,'История диалога очищена'); return
        if cmd in ('/limits','/mybudget'):
            user=self.db.user(uid); v=s.budgets.view(uid)
            lines=['Общий бюджет · все модели вместе',
                'За 3 часа: '+format_usd(v['h3_spent'])+' / '+format_usd(v['h3_nusd']),
                'За 24 часа: '+format_usd(v['day_spent'])+' / '+format_usd(v['day_nusd']),
                'Зарезервировано: '+format_usd(v['held'])]
            if v['unpriced']: lines.append('Есть расходы, требующие сверки владельцем')
            if s.owner(uid) or user['unlimited_until']==-1 or user['unlimited_until']>s.clock(): lines.append('Действует безлимит')
            else:
                remaining=max(0,min(v['h3_nusd']-v['h3_spent'],v['day_nusd']-v['day_spent'])-v['held'])
                lines.append('Доступно сейчас: '+format_usd(remaining))
            lines.append('Учет в USD по тарифам и данным API. Число сообщений не ограничивается')
            await self.send(chat,'\n'.join(lines)); return
        if not s.admin(uid): raise Denied('Нет прав администратора')
        if cmd=='/admin':
            await self.send(chat,ADMIN_HELP,[[{'text':'Расходы','callback_data':'usage'},{'text':'Пользователи','callback_data':'users'}],[{'text':'Общий API-ключ','callback_data':'key'}]]); return
        owner_commands={'/key','/catalog','/setmodel','/map','/enable','/adminrole','/global','/review','/object_delete','/budget_default','/price','/settle'}
        if cmd in owner_commands and not s.owner(uid): raise Denied('Только владелец')
        if cmd=='/key':
            self.pending[uid]=time.time()+120
            await self.send(chat,'Пришлите новый ключ AnyModel отдельным сообщением в течение 2 минут. Сообщение будет удалено, ключ зашифрован. /cancel - отмена'); return
        if cmd=='/catalog':
            try:
                key=self.vault.get('anymodel')
                r=await self.http.get('https://anymodel.org/v1/models',headers={'Authorization':'Bearer '+key})
                if r.status_code!=200: raise ProviderError('catalog_unavailable')
                models=[x['id'] for x in r.json()['data'] if x['id'].startswith(('glm/glm-','qwen/qwen','ds/deepseek-'))]
                self.db.set('catalog',models); self.db.set('catalog_at',time.time())
                await self.send(chat,'Доступные китайские ID (заявлены API):\n'+'\n'.join(models))
            except Exception: raise Denied('Не удалось получить каталог AnyModel. Проверьте ключ') from None
            return
        if cmd=='/budget_default':
            if len(args)==1:
                v=self.db.setting('default_budget',DEFAULT_BUDGET)
                await self.send(chat,'Бюджет по умолчанию на пользователя: '+format_usd(v['h3_nusd'])+' / 3 ч, '+format_usd(v['day_nusd'])+' / 24 ч'); return
            if len(args)!=3: raise ValueError('/budget_default USD_3H USD_24H')
            v={'h3_nusd':usd_to_nusd(args[1]),'day_nusd':usd_to_nusd(args[2])}
            self.db.set('default_budget',v); self.audit(uid,'budget_default',json.dumps(v))
            await self.send(chat,'Общий бюджет по умолчанию обновлен. Личные назначения сохранены'); return
        if cmd=='/price':
            if len(args)!=4: raise ValueError('/price MODEL INPUT_USD_PER_1M OUTPUT_USD_PER_1M')
            m=self.db.model(args[1]); m['input_price']=str(decimal(args[2]));m['output_price']=str(decimal(args[3]))
            validate_model(m);self.db.put_model(args[1],m);self.audit(uid,'price_changed',args[1])
            await self.send(chat,'Тариф обновлен для будущих запросов. Уже зарезервированные считаются по прежнему тарифу'); return
        if cmd=='/unsettled':
            data=self.db.rows("SELECT r.id request_id,s.uid,r.model,s.state,s.at,s.reserved_nusd FROM spending s JOIN requests r ON r.update_id=s.update_id WHERE s.state IN ('unknown','unpriced') OR (s.state='held' AND r.status='interrupted') ORDER BY r.id DESC LIMIT 30")
            await self.send(chat,json.dumps(data,ensure_ascii=False,indent=2)); return
        if cmd=='/settle':
            if len(args)!=3: raise ValueError('/settle REQUEST_ID USD')
            amount=usd_to_nusd(args[2]);s.budgets.manual_settle(int(args[1]),amount)
            self.audit(uid,'cost_reconciled',args[1]+':'+str(amount))
            await self.send(chat,'Расход сверен: '+format_usd(amount)); return
        if cmd=='/modelconfig': await self.send(chat,json.dumps(self.db.model(args[1]),ensure_ascii=False,indent=2)); return
        if cmd=='/map':
            key=args[1]; m=self.db.model(key); m['api_model']=args[2]
            validate_model(m); m['enabled']=False; m['approval_required']=True
            m['input_price']=None; m['output_price']=None
            m['note']='Соответствие изменено владельцем через /map'
            self.db.put_model(key,m); self.audit(uid,'model_mapped',key)
            await self.send(chat,'Соответствие сохранено. Обновите /catalog, затем /enable '+key+' 1. Тариф сброшен в unknown до проверки'); return
        if cmd=='/setmodel':
            key=args[1]; m=validate_model(json.loads(text.split(None,2)[2]))
            self.db.model(key)  # Existing logical model only
            m['enabled']=False; m['approval_required']=True
            self.db.put_model(key,m); self.audit(uid,'model_changed',key)
            await self.send(chat,'Настройки сохранены. Для проверки каталога /catalog, затем /enable MODEL 1'); return
        if cmd=='/enable':
            key=args[1]; value=int(args[2]); m=self.db.model(key)
            if value not in (0,1): raise ValueError('0 или 1')
            if value and (m['api_model'] not in self.db.setting('catalog',[]) or self.db.setting('catalog_at',0)<time.time()-86400):
                raise Denied('Сначала /catalog: ID должен присутствовать в свежем каталоге AnyModel')
            if value: rates(m)
            m['enabled']=bool(value); m['approval_required']=False if value else m.get('approval_required',False)
            self.db.put_model(key,m); self.audit(uid,'model_enabled',key)
            await self.send(chat,'Сохранено: '+m['name']+' → '+m['api_model']); return
        if cmd=='/users': await self.send(chat,json.dumps(self.db.rows('SELECT * FROM users ORDER BY id DESC LIMIT 30'),ensure_ascii=False,indent=2)); return
        if cmd=='/user': await self.send(chat,json.dumps(self.db.user(int(args[1])),ensure_ascii=False,indent=2)); return
        if cmd=='/usage':
            data=self.db.rows("SELECT r.model,count(*) diagnostic_request_count,sum(r.input_tokens) input,sum(r.output_tokens) output,sum(CASE WHEN s.state='settled' THEN s.amount_nusd ELSE 0 END) charged_nusd,sum(CASE WHEN s.state!='settled' THEN s.reserved_nusd ELSE 0 END) held_nusd,sum(s.state!='settled') unsettled FROM requests r JOIN spending s ON s.update_id=r.update_id GROUP BY r.model")
            await self.send(chat,'Расчет по usage AnyModel. Суммы в nanoUSD (1 USD = 1 000 000 000), число запросов только для диагностики:\n'+json.dumps(data,ensure_ascii=False,indent=2)); return
        if cmd=='/global':
            if args[1]=='maintenance': value=json.loads(args[2]); assert type(value) is bool
            elif args[1]=='timezone':
                from zoneinfo import ZoneInfo
                ZoneInfo(args[2]); value=args[2]
            else: raise ValueError('maintenance или timezone')
            self.db.set(args[1],value); self.audit(uid,'global',args[1]); await self.send(chat,'Настройка сохранена'); return
        if cmd=='/kb':
            term=text.split(None,1)[1]
            data=self.db.rows('SELECT id,source,event_date,public_from,access,status,substr(text,1,300) text FROM knowledge WHERE text LIKE ? LIMIT 10',('%'+term+'%',))
            await self.send(chat,json.dumps(data,ensure_ascii=False,indent=2)); return
        if cmd=='/inspect': await self.send(chat,json.dumps(self.db.rows('SELECT * FROM knowledge WHERE id=?',(args[1],)),ensure_ascii=False,indent=2)); return
        if cmd=='/review':
            from .knowledge import put
            row=self.db.rows('SELECT * FROM knowledge WHERE id=?',(args[1],))[0]
            row.update(event_date=args[2],public_from=args[3],access=args[4],status=args[5]); put(self.db,**row)
            self.audit(uid,'knowledge_reviewed',args[1]); await self.send(chat,'Метаданные обновлены'); return
        if cmd=='/objects': await self.send(chat,json.dumps(self.db.rows('SELECT * FROM objects ORDER BY id DESC LIMIT 30'),ensure_ascii=False,indent=2)); return
        if cmd=='/object_delete': self.db.execute('DELETE FROM objects WHERE id=?',(int(args[1]),)); await self.send(chat,'Удалено'); return
        if cmd not in ('/budget','/limit','/access','/unlimited','/reset','/ban','/filter','/adminrole'): raise ValueError('Неизвестная команда. /admin')
        target=int(args[1]); self.db.user(target)
        if target==s.owner_id and not s.owner(uid): raise Denied('Настройки владельца защищены')
        if not s.owner(uid) and self.db.user(target)['role']=='admin': raise Denied('Администратором управляет только владелец')
        if cmd in ('/budget','/limit'):
            if len(args)==3 and args[2]=='default':
                self.db.execute('DELETE FROM budgets WHERE uid=?',(target,))
            elif len(args)==4:
                h3,day=usd_to_nusd(args[2]),usd_to_nusd(args[3])
                self.db.execute('INSERT OR REPLACE INTO budgets VALUES(?,?,?)',(target,h3,day))
            else: raise ValueError('Лимиты теперь денежные: /budget ID USD_3H USD_24H')
        elif cmd=='/access':
            self.db.model(args[2]);allowed=int(args[3])
            if allowed not in (0,1): raise ValueError('0 или 1')
            # Legacy h3/day columns retained solely for v0.1 compatibility, never used as quotas.
            self.db.execute('INSERT INTO limits VALUES(?,?,0,0,?) ON CONFLICT(uid,model) DO UPDATE SET allowed=excluded.allowed',(target,args[2],allowed))
        elif cmd=='/unlimited':
            until=-1 if args[2]=='forever' else 0 if args[2]=='off' else time.time()+max(0,float(args[2]))*3600
            self.db.execute('UPDATE users SET unlimited_until=? WHERE id=?',(until,target))
        elif cmd=='/reset':
            if len(args)>2 and args[2]!='all': raise ValueError('/reset ID - сброс общего бюджета всех моделей')
            # Pending/unknown reservations always survive resets. Actual expense history is immutable.
            self.db.set('budget_reset:'+str(target),s.clock())
        else:
            value=int(args[2])
            if value not in (0,1): raise ValueError('0 или 1')
            column={'/ban':'blocked','/filter':'filter_on','/adminrole':'role'}[cmd]
            if cmd=='/adminrole': value='admin' if value else 'user'
            self.db.execute('UPDATE users SET '+column+'=? WHERE id=?',(value,target))
        self.audit(uid,cmd,target); await self.send(chat,'Сохранено')

    async def run(self):
        # Polling is sequential per batch, answers concurrent across users (bounded batch 20).
        offset=self.db.setting('telegram_offset',0)
        await self.api('deleteWebhook',drop_pending_updates=False)
        try:
            while True:
                try:
                    updates=await self.api('getUpdates',offset=offset,timeout=45,limit=20,allowed_updates=['message','callback_query'])
                    if updates:
                        await asyncio.gather(*(self.handle(u) for u in updates))
                        offset=max(u['update_id'] for u in updates)+1
                        self.db.set('telegram_offset',offset)
                except (httpx.HTTPError,RuntimeError):
                    log.error('telegram_poll_failed'); await asyncio.sleep(3)
        finally: await self.http.aclose()
