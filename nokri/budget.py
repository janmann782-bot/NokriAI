"""Shared rolling monetary budgets. All arithmetic uses integer nanodollars."""
import json
from decimal import Decimal, InvalidOperation, ROUND_CEILING

SCALE = 1_000_000_000
DEFAULT_BUDGET = {'h3_nusd':10_000_000, 'day_nusd':30_000_000}  # $0.01 / $0.03

class BudgetError(ValueError):
    pass

def decimal(value):
    try:
        d=Decimal(str(value).replace(',','.'))
    except (InvalidOperation,ValueError):
        raise BudgetError('Нужно конечное неотрицательное число') from None
    if not d.is_finite() or d<0: raise BudgetError('Нужно конечное неотрицательное число')
    return d

def usd_to_nusd(value):
    d=decimal(value)
    if d>1_000_000: raise BudgetError('Бюджет должен быть не больше 1 000 000 USD')
    exact=d*SCALE
    if exact!=exact.to_integral_value(): raise BudgetError('Точность суммы - до 9 знаков после точки')
    return int(exact)

def format_usd(nusd):
    return '$'+format(Decimal(nusd)/SCALE,'.9f').rstrip('0').rstrip('.')

def rates(model):
    if model.get('input_price') is None or model.get('output_price') is None:
        raise BudgetError('Тариф модели не настроен. Владелец должен задать /price перед ее использованием')
    a,b=decimal(model['input_price']),decimal(model['output_price'])
    if max(a,b)>1_000_000: raise BudgetError('Слишком большой тариф')
    return a,b

def token_cost(input_tokens,output_tokens,input_rate,output_rate):
    # USD/1M tokens -> integer nanoUSD, rounded up once per request.
    return int(((Decimal(input_tokens)*input_rate+Decimal(output_tokens)*output_rate)*1000).to_integral_value(rounding=ROUND_CEILING))

def quote(model,messages):
    a,b=rates(model)
    # Conservative estimate, not an upstream billing guarantee: a UTF-8 byte per token,
    # plus message framing and a fixed margin. Full possible output is reserved.
    n=len(json.dumps(messages,ensure_ascii=False).encode('utf-8'))+32*len(messages)+2048
    max_output=model.get('max_output_tokens',4096)
    estimate=token_cost(n,max_output,a,b)
    if estimate>9_000_000_000_000_000: raise BudgetError('Слишком большой резерв запроса')
    return estimate,str(a),str(b)

def actual_cost(usage,input_rate,output_rate):
    if not isinstance(usage,dict): return None
    a,b=usage.get('prompt_tokens'),usage.get('completion_tokens')
    if type(a) is not int or type(b) is not int or min(a,b)<0 or max(a,b)>10**10:
        return None
    amount=token_cost(a,b,Decimal(input_rate),Decimal(output_rate))
    return amount if amount<=9_000_000_000_000_000 else None

class Budgets:
    def __init__(self,db,clock): self.db,self.clock=db,clock

    @staticmethod
    def setting(c,key,default):
        row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def caps(self,c,uid):
        row=c.execute('SELECT h3_nusd,day_nusd FROM budgets WHERE uid=?',(uid,)).fetchone()
        return dict(row) if row else self.setting(c,'default_budget',DEFAULT_BUDGET)

    def totals(self,c,uid,now):
        reset=self.setting(c,'budget_reset:'+str(uid),0)
        # Pending/uncertain API calls survive resets and remain reserved until reconciliation.
        held=c.execute("SELECT coalesce(sum(reserved_nusd),0) FROM spending WHERE uid=? AND state IN ('held','unknown','unpriced')",(uid,)).fetchone()[0]
        uncertain=c.execute("SELECT count(*) FROM spending WHERE uid=? AND state='unpriced'",(uid,)).fetchone()[0]
        amounts=[]
        for sec in (10800,86400):
            spent=c.execute("SELECT coalesce(sum(amount_nusd),0) FROM spending WHERE uid=? AND state='settled' AND at>?",(uid,max(now-sec,reset))).fetchone()[0]
            amounts.append(spent)
        return {'h3_spent':amounts[0],'day_spent':amounts[1],'held':held,'unpriced':uncertain}

    def view(self,uid):
        with self.db.transaction() as c:
            return {**self.caps(c,uid),**self.totals(c,uid,self.clock())}

    def settle(self,update_id,usage=None,zero=False,unknown=False):
        with self.db.transaction() as c:
            row=c.execute('SELECT * FROM spending WHERE update_id=?',(str(update_id),)).fetchone()
            if not row or row['state']=='settled': return
            amount=0 if zero else actual_cost(usage,row['input_rate'],row['output_rate'])
            state='unknown' if unknown or amount is None else 'settled'
            # Keep the reservation if the upstream did not return usable accounting.
            c.execute('UPDATE spending SET state=?,amount_nusd=?,settled_at=? WHERE update_id=?',
                (state,amount if state=='settled' else None,self.clock(),str(update_id)))
            c.execute('UPDATE requests SET cost=? WHERE update_id=?',
                (amount/SCALE if state=='settled' else None,str(update_id)))

    def manual_settle(self,request_id,amount_nusd):
        with self.db.transaction() as c:
            r=c.execute('SELECT update_id,status FROM requests WHERE id=?',(request_id,)).fetchone()
            if not r: raise BudgetError('Запрос не найден')
            row=c.execute('SELECT state FROM spending WHERE update_id=?',(r['update_id'],)).fetchone()
            if not row or row['state']=='settled': raise BudgetError('Нет неизвестного списания для сверки')
            if row['state']=='held' and r['status']!='interrupted': raise BudgetError('Запрос еще выполняется. Дождитесь ответа или восстановления после перезапуска')
            c.execute("UPDATE spending SET state='settled',amount_nusd=?,settled_at=? WHERE update_id=?",(amount_nusd,self.clock(),r['update_id']))
            c.execute('UPDATE requests SET cost=? WHERE update_id=?',(amount_nusd/SCALE,r['update_id']))

    def migrate_legacy(self):
        # Actual legacy costs can be preserved. Unknown legacy costs must not silently become free.
        with self.db.transaction() as c:
            rows=c.execute('SELECT r.* FROM requests r LEFT JOIN spending s ON s.update_id=r.update_id WHERE s.update_id IS NULL').fetchall()
            for r in rows:
                amount=None
                if r['cost'] is not None:
                    try: amount=int((decimal(r['cost'])*SCALE).to_integral_value(rounding=ROUND_CEILING))
                    except BudgetError: pass
                state='settled' if amount is not None else 'unpriced'
                c.execute('INSERT INTO spending(update_id,uid,at,state,amount_nusd,reserved_nusd,input_rate,output_rate) VALUES(?,?,?,?,?,?,?,?)',
                    (r['update_id'],r['uid'],r['at'],state,amount,0,'0','0'))
