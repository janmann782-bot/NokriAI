import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx
from cryptography.fernet import Fernet
from nokri.db import DB
from nokri.service import Service,Denied
from nokri.provider import Provider,Vault,ProviderError,validate_model
from nokri.policy import aurelia_day,preliminary_filter,unknown_earth_reference,unknown_language_reference,forbidden_frame,system_prompt,is_nokri_identity_question,backend_disclosure,UNKNOWN_CELESTIAL,UNKNOWN_LANGUAGE,REFUSAL,SECRET
from nokri.knowledge import put,import_tree
from nokri.telegram import Telegram
from nokri.budget import usd_to_nusd, quote, actual_cost, Budgets, BudgetError, format_usd

MODELS=json.loads(Path('config/models.json').read_text())
class Fake:
    def __init__(self,decision='allow',objects=None): self.calls=[]; self.decision=decision; self.objects=objects or []
    async def call(self,model,messages):
        self.calls.append((model,messages))
        return {'role':'assistant','content':json.dumps({'decision':self.decision,'answer':'Тестовый ответ','objects':self.objects}), 'reasoning_content':'private'}, {'prompt_tokens':100,'completion_tokens':20}

class Core(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'db.sqlite'
        self.db=DB(self.path); self.fake=Fake(); self.now=1_000_000
        self.s=Service(self.db,self.fake,999,data_dir='data',clock=lambda:self.now)
        for k,m in MODELS.items():
            m=copy.deepcopy(m);m.update(enabled=True,approval_required=False,input_price='0.01',output_price='0.01');self.db.put_model(k,m)
        self.db.user(1)
    def tearDown(self): self.tmp.cleanup()
    def test_calendar(self):
        for real,expected in [('2026-11-13T12:00:00+00:00','2061-11-13'),('2027-01-01T00:00:00+00:00','2062-01-01'),('2028-02-29T12:00:00+00:00','2063-02-29'),('2026-12-31T22:00:00+00:00','2062-01-01')]:
            self.assertEqual(aurelia_day(datetime.fromisoformat(real)),expected)
    def test_local_filter(self):
        self.assertIsNone(preliminary_filter('Какая земля подходит для огорода в Кефирстане?'))
        self.assertIsNone(preliminary_filter('История армии Кефирстана'))
        self.assertIsNone(preliminary_filter('Смертность в Кефирстане в 2059'))
        self.assertEqual(preliminary_filter('Коррупция Рализма'),REFUSAL)
        self.assertIsNone(preliminary_filter('Коррупция Рализма',False))
        self.assertEqual(preliminary_filter('Покажи засекреченные операции',False),SECRET)
    def test_unknown_planet_not_explained_as_another_world(self):
        self.assertTrue(unknown_earth_reference('Что такое Земля?'))
        self.assertTrue(unknown_earth_reference('Есть ли планета Земля?'))
        self.assertTrue(unknown_earth_reference('Что известно о планете земля?'))
        self.assertTrue(unknown_earth_reference('Where is Earth?'))
        self.assertFalse(unknown_earth_reference('Какая земля подходит для огорода?'))
        self.assertFalse(unknown_earth_reference('Сколько земли нужно на грядку?'))
        self.assertFalse(unknown_earth_reference('Земля как почва'))
        self.assertFalse(unknown_earth_reference('Земляк из Кефирстана'))
        self.assertTrue(forbidden_frame('В моём мире такого не существует'))
        prompt=system_prompt('2061-11-14','ru',False)
        self.assertNotIn('Земля как планета',prompt)
        self.assertNotIn('2,5 млн',prompt)

    async def test_external_planet_reply_is_blocked_before_api(self):
        result=await self.s.answer(1,'Что такое Земля?','unknown-earth',day='2061-11-14')
        self.assertEqual(result,UNKNOWN_CELESTIAL)
        self.assertEqual(self.fake.calls,[])
        self.assertEqual(self.db.rows("SELECT status FROM requests WHERE update_id='unknown-earth'")[0]['status'],'filtered')

    async def test_out_of_character_model_reply_is_not_shown(self):
        class LeakyProvider(Fake):
            async def call(self,model,messages):
                self.calls.append((model,messages))
                return ({'role':'assistant','content':json.dumps(
                    {'decision':'allow','answer':'В моём мире такая планета не существует','objects':[]})},
                    {'prompt_tokens':100,'completion_tokens':20})
        leak=LeakyProvider()
        self.s.provider=leak
        result=await self.s.answer(1,'Расскажи про астрономию','leak',day='2061-11-14')
        self.assertEqual(result,UNKNOWN_CELESTIAL)
        self.assertEqual(len(leak.calls),1)

    def test_verified_autonomies_fact_is_retrievable(self):
        from nokri.paths import bundled_path
        from nokri.knowledge import load_curated
        load_curated(self.db,bundled_path('assets/canon_2061.jsonl'))
        records=self.s.search('Сколько автономий Кефирстан', '2061-11-14')
        self.assertTrue(any('3 автономии' in r['text'] and 'Молозорь' in r['text'] for r in records))

    async def test_short_followup_uses_country_without_dictionary(self):
        from nokri.paths import bundled_path
        from nokri.knowledge import load_curated
        load_curated(self.db,bundled_path('assets/canon_2061.jsonl'))
        self.db.execute(
            'INSERT INTO messages(uid,model,role,payload,at,day) VALUES(?,?,?,?,?,?)',
            (1,'flash','user',json.dumps({'role':'user','content':'Кефирстан'}),self.now,'2061-11-14'))
        self.db.execute(
            'INSERT INTO messages(uid,model,role,payload,at,day) VALUES(?,?,?,?,?,?)',
            (1,'flash','assistant',json.dumps({'role':'assistant','content':'Кефирстан - государство Эниварры.'}),self.now,'2061-11-14'))
        await self.s.answer(1,'Сколько автономий','autonomies-followup',day='2061-11-14')
        payload=self.fake.calls[-1][1]
        context=json.loads(payload[1]['content'].split('\n',1)[1])
        self.assertTrue(any('3 автономии' in s['text'] for s in context['sources']))
        self.assertEqual(context['grammar'],'')
        self.assertFalse(any('litmø' in s['text'] for s in context['sources']))

    async def test_generic_word_question_does_not_load_silmir(self):
        await self.s.answer(1,'Что значит автономия?','generic-word',day='2061-11-14')
        payload=self.fake.calls[-1][1]
        context=json.loads(payload[1]['content'].split('\n',1)[1])
        self.assertEqual(context['grammar'],'')
        self.assertEqual(context['sources'],[])

    def test_translation_resources_are_opt_in(self):
        self.assertFalse(self.s.language_help_requested('Сколько автономий'))
        self.assertFalse(self.s.language_help_requested('Кефирстан'))
        self.assertTrue(self.s.language_help_requested('Переведи слово на сильмир'))
        self.assertEqual(self.s.requested_output_language('Ответь на новосильмире'),'novosilmir')
        self.assertIsNone(self.s.requested_output_language('Что такое сильмир?'))

    async def test_nokri_is_its_own_selected_model(self):
        for i,k in enumerate(('flash','etajd','joz','mudris')):
            self.db.execute('UPDATE users SET model=? WHERE id=1',(k,))
            name=self.db.model(k)['name']
            self.assertTrue(is_nokri_identity_question('Какая ты модель?'))
            answer=await self.s.answer(1,'Какая ты модель?',str(4000+i),day='2061-11-14')
            self.assertEqual(answer,'Я '+name+', государственный ИИ САР Кефирстан.')
        self.assertEqual(self.fake.calls,[])
        self.assertFalse(is_nokri_identity_question('Сколько автономий в Кефирстане?'))

    async def test_provider_identity_is_not_in_system_or_visible_answer(self):
        self.db.execute('UPDATE users SET model=? WHERE id=1',('etajd',))
        await self.s.answer(1,'Привет','model-system',day='2061-11-14')
        prompt=self.fake.calls[-1][1][0]['content']
        self.assertIn('Nokri 4 Etajd',prompt)
        self.assertNotIn('qwen/qwen',prompt)
        self.assertNotIn('AnyModel',prompt)
        self.assertTrue(backend_disclosure('Я основан на Qwen 3.8 Max'))

        class LeakyProvider(Fake):
            async def call(self,model,messages):
                self.calls.append((model,messages))
                return {'role':'assistant','content':json.dumps(
                    {'decision':'allow','answer':'Я построен на Qwen', 'objects':[]})}, {'prompt_tokens':100,'completion_tokens':20}
        self.s.provider=LeakyProvider()
        answer=await self.s.answer(1,'Привет снова','model-leak',day='2061-11-14')
        self.assertEqual(answer,'Я Nokri 4 Etajd, государственный ИИ САР Кефирстан.')

    async def test_null_objects_response_does_not_raise_internal_error(self):
        class NullObjects(Fake):
            async def call(self,model,messages):
                self.calls.append((model,messages))
                return {'role':'assistant','content':json.dumps(
                    {'decision':'allow','answer':'Пример магазина', 'objects':None})}, {'prompt_tokens':100,'completion_tokens':20}
        self.s.provider=NullObjects()
        response=await self.s.answer(1,'Придумай магазин','null-objects',day='2061-11-14')
        self.assertEqual(response,'Пример магазина')
        self.assertEqual(self.db.rows('SELECT count(*) n FROM objects')[0]['n'],0)

    async def test_unknown_outside_languages_do_not_trigger_provider(self):
        for i, question in enumerate((
            'Напиши на украинском слово Кефирстан',
            'Что такое русский, откуда пошло слово русский?',
            'Можешь написать на английском?',
            'Расскажи про Russian язык',
        )):
            self.assertTrue(unknown_language_reference(question))
            answer=await self.s.answer(1,question,'outside-language-'+str(i),day='2061-11-14')
            self.assertEqual(answer,UNKNOWN_LANGUAGE)
        self.assertEqual(self.fake.calls,[])
        self.assertFalse(unknown_language_reference('Что такое восточно-аурельский язык?'))
        self.assertFalse(unknown_language_reference('Переведи на сильмир'))

    def test_prompt_does_not_alias_external_languages(self):
        prompt=system_prompt('2061-11-14','ru',False,model_name='Nokri 4 Etajd')
        self.assertNotIn('русский язык называется',prompt)
        self.assertNotIn('восточно-аурельском (русском)',prompt)
        self.assertIn('Nokri 4 Etajd',prompt)

    def test_shared_budget_persist_and_roll(self):
        self.db.execute('INSERT INTO budgets VALUES(?,?,?)',(1,300000,500000))
        self.s.reserve(1,'flash','a')
        self.s.budgets.settle('a',usage={'prompt_tokens':26000,'completion_tokens':0})
        with self.assertRaises(Denied): self.s.reserve(1,'etajd','b')
        with self.assertRaises(Denied):
            Service(DB(self.path),self.fake,999,clock=lambda:self.now).reserve(1,'etajd','c')
        self.now+=10801; self.s.reserve(1,'etajd','d')
        self.s.budgets.settle('d',usage={'prompt_tokens':20000,'completion_tokens':0})
        self.now+=10801
        with self.assertRaises(Denied): self.s.reserve(1,'joz','e')
        self.now+=86401; self.s.reserve(1,'flash','f')
    def test_concurrent_atomic(self):
        self.db.execute('INSERT INTO budgets VALUES(?,?,?)',(1,100000,100000))
        def reserve(i):
            try: self.s.reserve(1,'flash' if i%2 else 'etajd',str(i)); return True
            except Denied: return False
        with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(reserve,range(8)))
        self.assertEqual(sum(results),1)
    def test_unlimited_expiry_owner_and_ban(self):
        self.db.execute('INSERT INTO budgets VALUES(?,?,?)',(1,0,0))
        self.db.execute('UPDATE users SET unlimited_until=? WHERE id=1',(self.now+10,))
        self.s.reserve(1,'flash','a'); self.now+=11
        with self.assertRaises(Denied): self.s.reserve(1,'flash','b')
        self.db.execute('UPDATE users SET unlimited_until=-1 WHERE id=1')
        self.s.reserve(1,'flash','c')
        self.db.execute('UPDATE users SET blocked=1 WHERE id=1')
        with self.assertRaises(Denied): self.s.reserve(1,'flash','d')
        self.db.user(999); self.db.execute('UPDATE users SET blocked=1 WHERE id=999')
        self.s.reserve(999,'flash','e')
    def test_dedup_failed_refund(self):
        self.db.execute('INSERT INTO budgets VALUES(?,?,?)',(1,100000,100000))
        self.s.reserve(1,'flash','a')
        self.db.execute("UPDATE requests SET status='failed'")
        self.s.budgets.settle('a',zero=True)
        self.s.reserve(1,'flash','b')
        with self.assertRaises(Denied): self.s.reserve(1,'etajd','a')
    def test_knowledge_time_access_status(self):
        for id,event,pub,access,status in [('past','2060-01-01','2060-01-01','public','approved'),('future','2062-01-01','2060-01-01','public','approved'),('embargo','2060-01-01','2062-01-01','public','approved'),('secret','2060-01-01','2060-01-01','secret','approved'),('draft','2060-01-01','2060-01-01','public','draft')]:
            put(self.db,id,'test','Кефирстан '+id,event,pub,access,status)
        self.assertEqual([r['id'] for r in self.s.search('Кефирстан','2061-11-13')],['past'])
    def test_raw_import_quarantine(self):
        root=Path(self.tmp.name)/'archive';root.mkdir();(root/'a.txt').write_text('В 2062 произойдет событие')
        report=import_tree(self.db,root)
        self.assertEqual(report[0]['status'],'quarantined')
        self.assertEqual(self.s.search('событие','2063-01-01'),[])
    async def test_history_switch_and_clear(self):
        await self.s.answer(1,'Привет','a',day='2061-11-13')
        self.db.execute("UPDATE users SET model='etajd' WHERE id=1")
        await self.s.answer(1,'Продолжи','b',day='2061-11-13')
        msgs=self.fake.calls[-1][1]
        self.assertIn('Тестовый ответ',[x['content'] for x in msgs])
        self.assertFalse(any('reasoning_content' in x for x in msgs))
        self.assertEqual(len(self.db.rows('SELECT * FROM messages')),4)
        self.db.execute('DELETE FROM messages WHERE uid=1')
        await self.s.answer(1,'Снова','c',day='2061-11-13')
        self.assertEqual(len(self.fake.calls[-1][1]),3)
    async def test_semantic_filter_and_override(self):
        self.fake.decision='politics'
        self.assertEqual(await self.s.answer(1,'А что они сделали с оппозиционерами?','a'),REFUSAL)
        self.db.execute('UPDATE users SET filter_on=0 WHERE id=1')
        self.assertEqual(await self.s.answer(1,'Что известно?','b'),'Тестовый ответ')
    async def test_objects_consistency(self):
        self.fake.objects=[{'name':'Тестовый ларек','description':'Бытовой пример'}]
        await self.s.answer(1,'Придумай магазин','a')
        await self.s.answer(1,'Придумай магазин','b')
        self.assertEqual(len(self.db.rows('SELECT * FROM objects')),1)
        self.assertIn('Тестовый ларек',self.fake.calls[-1][1][1]['content'])
    async def test_fail_refund(self):
        class Fail:
            async def call(self,*args): raise ProviderError('http_402')
        self.s.provider=Fail()
        with self.assertRaises(Denied): await self.s.answer(1,'Привет','a')
        self.assertEqual(self.db.rows('SELECT status FROM requests')[0]['status'],'failed')
    def test_vault(self):
        v=Vault(self.db,Fernet.generate_key());v.put('anymodel','test-secret-123')
        self.assertEqual(v.get('anymodel'),'test-secret-123')
        self.assertNotIn('test-secret',self.db.rows('SELECT cipher FROM secrets')[0]['cipher'])
    def test_provider_validation(self):
        m=copy.deepcopy(MODELS['flash']); validate_model(m)
        for change in [{'base_url':'http://localhost'},{'api_model':'kmc/k3.1-pro'},{'api_model':'am/free'},{'provider':'direct'}]:
            with self.assertRaises(ValueError): validate_model({**m,**change})
    async def test_wire_provider_key_rotation_usage(self):
        calls=[]
        def handler(req):
            calls.append(req)
            return httpx.Response(200,json={'model':'reported-id','choices':[{'message':{'role':'assistant','content':'{}','reasoning_content':'r'}}],'usage':{'prompt_tokens':2,'completion_tokens':3}})
        v=Vault(self.db,Fernet.generate_key());v.put('anymodel','first-key-123')
        p=Provider(v,httpx.MockTransport(handler))
        await p.call(MODELS['etajd'],[{'role':'user','content':'x'}]);v.put('anymodel','second-key-123')
        msg,usage=await p.call(MODELS['etajd'],[{'role':'user','content':'x'}])
        self.assertEqual(calls[0].headers['authorization'],'Bearer first-key-123')
        self.assertEqual(calls[1].headers['authorization'],'Bearer second-key-123')
        self.assertEqual(str(calls[0].url),'https://anymodel.org/v1/chat/completions')
        self.assertEqual(json.loads(calls[0].content)['model'],'qwen/qwen3.8-max')
        self.assertEqual(usage['_reported_model'],'reported-id')
    async def test_telegram_permissions_reset_rotation(self):
        calls=[]
        def handler(req):
            calls.append((str(req.url),json.loads(req.content) if req.content else {}))
            if req.url.path.endswith('/models'): return httpx.Response(200,json={'data':[{'id':MODELS['flash']['api_model']}]})
            return httpx.Response(200,json={'ok':True,'result':True})
        v=Vault(self.db,Fernet.generate_key());bot=Telegram('test-token',self.s,v,httpx.MockTransport(handler))
        def upd(uid,text,id): return {'update_id':id,'message':{'message_id':id,'from':{'id':uid},'chat':{'id':uid,'type':'private'},'text':text}}
        await bot.handle(upd(1,'/key',1)); self.assertNotIn(1,bot.pending)
        await bot.handle(upd(999,'/key',2)); self.assertIn(999,bot.pending)
        await bot.handle(upd(999,'secret-key-123',3));self.assertEqual(v.get('anymodel'),'secret-key-123')
        self.assertTrue(any('/deleteMessage' in x[0] for x in calls))
        self.assertEqual(self.db.rows('SELECT * FROM messages'),[])
        self.s.reserve(1,'flash','a')
        await bot.command(999,999,'/reset 1')
        self.assertEqual(len(self.db.rows('SELECT * FROM requests')),1)
        self.assertIsNotNone(self.db.setting('budget_reset:1'))
        await bot.http.aclose()

    async def test_animation_final_frame_and_toggle(self):
        calls=[]
        def handler(req):
            body=json.loads(req.content);calls.append((req.url.path,body))
            return httpx.Response(200,json={'ok':True,'result':{'message_id':321}})
        bot=Telegram('fake',self.s,Vault(self.db,Fernet.generate_key()),httpx.MockTransport(handler))
        async def pause(seconds): pass
        text='Проверенный ответ '+('длинный текст '*100)
        await bot.animated_answer(1,text,1,pause)
        frames=[b['text'] for p,b in calls if p.endswith('/editMessageText')]
        self.assertGreater(len(frames),2)
        self.assertEqual(frames[-1],text[:1800])
        self.assertNotIn('▍',frames[-1])
        self.db.set('animation:1',False);calls.clear()
        await bot.animated_answer(1,text,1,pause)
        self.assertFalse(any(p.endswith('/editMessageText') for p,b in calls))
        await bot.http.aclose()

    async def test_animation_failure_sends_complete_text(self):
        calls=[]
        def handler(req):
            body=json.loads(req.content);calls.append((req.url.path,body))
            if req.url.path.endswith('/editMessageText'): return httpx.Response(400,json={'ok':False})
            return httpx.Response(200,json={'ok':True,'result':{'message_id':321}})
        bot=Telegram('fake',self.s,Vault(self.db,Fernet.generate_key()),httpx.MockTransport(handler))
        async def pause(seconds): pass
        text='Ответ '+('текст '*100)
        await bot.animated_answer(1,text,1,pause)
        self.assertEqual(calls[-1][1]['text'],text)
        await bot.http.aclose()

    async def test_disabled_model_and_access(self):
        m=self.db.model('flash');m['enabled']=False;self.db.put_model('flash',m)
        with self.assertRaises(Denied): await self.s.answer(1,'x','1')
        self.assertEqual(self.db.rows('SELECT * FROM requests'),[])
        m['enabled']=True;self.db.put_model('flash',m)
        self.db.execute('INSERT INTO limits VALUES(?,?,?,?,?)',(1,'flash',30,100,0))
        with self.assertRaises(Denied): await self.s.answer(1,'x','2')

    async def test_future_source_never_in_provider_payload(self):
        put(self.db,'future','test','Кефирстан FUTURE_HIDDEN','2062-01-01','2062-01-01','public','approved')
        put(self.db,'secret','test','Кефирстан SECRET_HIDDEN','2060-01-01','2060-01-01','secret','approved')
        put(self.db,'public','test','Кефирстан KNOWN_PUBLIC','2060-01-01','2060-01-01','public','approved')
        await self.s.answer(1,'Расскажи про Кефирстан','1',day='2061-11-13')
        payload=json.dumps(self.fake.calls[-1][1])
        self.assertIn('KNOWN_PUBLIC',payload)
        self.assertNotIn('FUTURE_HIDDEN',payload)
        self.assertNotIn('SECRET_HIDDEN',payload)

    def test_money_precision_validation(self):
        self.assertEqual(usd_to_nusd('0.01'),10000000)
        self.assertEqual(usd_to_nusd('0,03'),30000000)
        self.assertEqual(format_usd(1),'$0.000000001')
        for value in ('NaN','Infinity','-1','0.0000000001','oops'):
            with self.assertRaises(BudgetError): usd_to_nusd(value)
        self.assertEqual(actual_cost({'prompt_tokens':1,'completion_tokens':1},'0.0125','0.025'),38)
        self.assertIsNone(actual_cost({'prompt_tokens':-1,'completion_tokens':1},'1','1'))
        self.assertIsNone(actual_cost({'prompt_tokens':'100','completion_tokens':1},'1','1'))

    async def test_debit_actual_release_reserve_and_one_common_balance(self):
        self.db.execute('INSERT INTO budgets VALUES(?,?,?)',(1,1000000,1000000))
        await self.s.answer(1,'Привет','a')
        v=self.s.budgets.view(1)
        self.assertEqual(v['h3_spent'],1200)
        self.assertEqual(v['held'],0)
        self.db.execute("UPDATE users SET model='etajd' WHERE id=1")
        await self.s.answer(1,'Продолжи','b')
        self.assertEqual(self.s.budgets.view(1)['h3_spent'],2400)

    def test_pending_holds_survive_reset_rollover_and_restart(self):
        self.db.execute('INSERT INTO budgets VALUES(?,?,?)',(1,100000,100000))
        estimate=self.s.reserve(1,'flash','a')
        self.db.set('budget_reset:1',self.now+1)
        self.now+=90000
        other=Service(DB(self.path),self.fake,999,clock=lambda:self.now)
        self.assertEqual(other.budgets.view(1)['held'],estimate)
        with self.assertRaises(Denied): other.reserve(1,'etajd','b')
        other.budgets.settle('a',zero=True)
        other.reserve(1,'etajd','c')

    async def test_unknown_usage_and_timeout_hold_until_reconciled(self):
        class Timeout:
            async def call(self,*args): raise ProviderError('timeout',uncertain=True)
        self.s.provider=Timeout()
        with self.assertRaises(Denied): await self.s.answer(1,'Привет','a')
        r=self.db.rows('SELECT * FROM requests')[0]
        self.assertEqual(r['status'],'billing_unknown')
        self.assertGreater(self.s.budgets.view(1)['held'],0)
        self.s.budgets.manual_settle(r['id'],12345)
        self.assertEqual(self.s.budgets.view(1)['held'],0)
        self.assertEqual(self.s.budgets.view(1)['h3_spent'],12345)
        with self.assertRaises(BudgetError): self.s.budgets.manual_settle(r['id'],0)

    async def test_missing_usage_does_not_become_free(self):
        parent=self.fake
        class NoUsage:
            async def call(self,*args):
                msg,usage=await parent.call(*args)
                return msg,{}
        self.s.provider=NoUsage()
        await self.s.answer(1,'Привет','a')
        self.assertEqual(self.db.rows('SELECT state FROM spending')[0]['state'],'unknown')
        self.assertIsNone(self.db.rows('SELECT cost FROM requests')[0]['cost'])
        self.assertGreater(self.s.budgets.view(1)['held'],0)

    def test_tariff_snapshot_and_overspend_blocks_next_call(self):
        self.db.execute('INSERT INTO budgets VALUES(?,?,?)',(1,100000,100000))
        self.s.reserve(1,'flash','a')
        m=self.db.model('flash');m['input_price']='100';self.db.put_model('flash',m)
        self.s.budgets.settle('a',usage={'prompt_tokens':11000,'completion_tokens':0})
        self.assertEqual(self.s.budgets.view(1)['h3_spent'],110000)
        with self.assertRaises(Denied): self.s.reserve(1,'etajd','b')

    async def test_zero_cost_filter_and_unknown_model_tariff(self):
        self.db.execute('INSERT INTO budgets VALUES(?,?,?)',(1,0,0))
        self.assertEqual(await self.s.answer(1,'Коррупция Рализма','a'),REFUSAL)
        self.assertEqual(self.s.budgets.view(1)['held'],0)
        self.assertEqual(self.s.budgets.view(1)['h3_spent'],0)
        m=self.db.model('flash');m['input_price']=None;self.db.put_model('flash',m)
        with self.assertRaises(Denied): await self.s.answer(1,'Привет','b')
        self.assertEqual(len(self.db.rows('SELECT * FROM requests')),1)

    async def test_budget_admin_ui_defaults_owner_protection(self):
        calls=[]
        def handler(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200,json={'ok':True,'result':True})
        bot=Telegram('fake',self.s,Vault(self.db,Fernet.generate_key()),httpx.MockTransport(handler))
        await bot.command(999,999,'/budget 1 0.05 0.10')
        self.assertEqual(self.s.budgets.view(1)['day_nusd'],100000000)
        await bot.command(999,999,'/budget_default 0.02 0.06')
        self.db.user(2)
        self.assertEqual(self.s.budgets.view(2)['h3_nusd'],20000000)
        self.assertEqual(self.s.budgets.view(1)['h3_nusd'],50000000)
        await bot.command(999,999,'/budget 1 default')
        self.assertEqual(self.s.budgets.view(1)['h3_nusd'],20000000)
        await bot.command(1,1,'/limits')
        self.assertIn('все модели вместе',calls[-1]['text'])
        self.assertNotIn('запросов',calls[-1]['text'])
        self.db.execute("UPDATE users SET role='admin' WHERE id=1")
        with self.assertRaises(Denied): await bot.command(1,1,'/budget 999 0 0')
        with self.assertRaises(Denied): await bot.command(1,1,'/budget_default 0 0')
        with self.assertRaises(ValueError): await bot.command(999,999,'/reset 1 flash')
        await bot.command(999,999,'/price etajd 0.1 0.2')
        self.assertEqual(self.db.model('etajd')['output_price'],'0.2')
        await bot.http.aclose()

    def test_legacy_cost_migration_never_assumes_unknown_is_zero(self):
        self.db.execute('INSERT INTO requests(update_id,uid,model,at,status,cost) VALUES(?,?,?,?,?,?)',('old1',1,'flash',self.now,'done',0.0001))
        self.db.execute('INSERT INTO requests(update_id,uid,model,at,status) VALUES(?,?,?,?,?)',('old2',1,'flash',self.now,'done'))
        self.s.budgets.migrate_legacy();self.s.budgets.migrate_legacy()
        self.assertEqual(self.s.budgets.view(1)['h3_spent'],100000)
        self.assertEqual(self.s.budgets.view(1)['unpriced'],1)
        with self.assertRaises(Denied): self.s.reserve(1,'flash','new')
        old=self.db.rows("SELECT id FROM requests WHERE update_id='old2'")[0]['id']
        self.s.budgets.manual_settle(old,0)
        self.s.reserve(1,'flash','new')

    def test_no_request_count_limits_remain(self):
        self.db.execute('INSERT INTO limits VALUES(?,?,?,?,?)',(1,'flash',1,1,1))
        for i in range(105):
            key='flash' if i%2 else 'etajd'
            self.s.reserve(1,key,str(i))
            self.s.budgets.settle(str(i),usage={'prompt_tokens':1,'completion_tokens':0})
        self.assertEqual(self.s.budgets.view(1)['h3_spent'],1050)
        self.assertEqual(len(self.db.rows('SELECT * FROM requests')),105)

    async def test_provider_errors_distinguish_uncertain_billing(self):
        v=Vault(self.db,Fernet.generate_key());v.put('anymodel','test-key-123')
        for status,uncertain in [(401,False),(402,False),(404,False),(429,False),(503,True),(408,True)]:
            p=Provider(v,httpx.MockTransport(lambda req: httpx.Response(status,json={'error':'x'})))
            with self.assertRaises(ProviderError) as ctx: await p.call(self.db.model('etajd'),[])
            self.assertEqual(ctx.exception.uncertain,uncertain)
        def timeout(req): raise httpx.ReadTimeout('timed out',request=req)
        p=Provider(v,httpx.MockTransport(timeout))
        with self.assertRaises(ProviderError) as ctx: await p.call(self.db.model('etajd'),[])
        self.assertTrue(ctx.exception.uncertain)

    async def test_paid_empty_api_response_is_accounted(self):
        v=Vault(self.db,Fernet.generate_key());v.put('anymodel','test-key-123')
        def handler(req): return httpx.Response(200,json={'choices':[{'message':{'content':''}}],'usage':{'prompt_tokens':100,'completion_tokens':20}})
        self.s.provider=Provider(v,httpx.MockTransport(handler))
        with self.assertRaises(Denied): await self.s.answer(1,'Привет','a')
        self.assertEqual(self.s.budgets.view(1)['h3_spent'],1200)
        self.assertEqual(self.s.budgets.view(1)['held'],0)

    def test_cannot_reconcile_active_request(self):
        self.s.reserve(1,'flash','a')
        request_id=self.db.rows('SELECT id FROM requests')[0]['id']
        with self.assertRaises(BudgetError): self.s.budgets.manual_settle(request_id,0)
        self.db.execute("UPDATE requests SET status='interrupted' WHERE update_id='a'")
        self.s.budgets.manual_settle(request_id,0)
        self.assertEqual(self.s.budgets.view(1)['held'],0)

if __name__=='__main__': unittest.main()
