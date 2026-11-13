import json
import httpx
from cryptography.fernet import Fernet
from .budget import decimal, rates

class ProviderError(Exception):
    def __init__(self,code,uncertain=False,usage=None):
        super().__init__(code)
        self.uncertain,self.usage=uncertain,usage

def validate_model(m):
    if m['base_url'] != 'https://anymodel.org/v1' or m['provider'] != 'anymodel':
        raise ValueError('Разрешен только AnyModel https://anymodel.org/v1')
    if not m['api_model'].startswith(('glm/glm-','qwen/qwen','ds/deepseek-')):
        raise ValueError('Разрешены только GLM, Qwen и DeepSeek. Kimi исключен')
    for field in ('input_price','output_price'):
        if m.get(field) is not None and decimal(m[field])>1_000_000: raise ValueError('Слишком большой тариф')
    max_output=m.get('max_output_tokens',4096)
    if type(max_output) is not int or not 128<=max_output<=8192: raise ValueError('max_output_tokens: от 128 до 8192')
    if not isinstance(m.get('params',{}),dict): raise ValueError('params должен быть объектом')
    if set(m.get('params',{})) - {'thinking','reasoning_effort','enable_thinking'}:
        raise ValueError('Неизвестный API параметр')
    return m

class Vault:
    def __init__(self, db, master):
        self.db, self.fernet = db, Fernet(master.encode() if isinstance(master,str) else master)
    def put(self, provider, key):
        if provider != 'anymodel': raise ValueError('Неизвестный провайдер')
        if not 8 <= len(key) <= 512 or any(x.isspace() for x in key): raise ValueError('Некорректный ключ')
        self.db.execute('INSERT OR REPLACE INTO secrets VALUES(?,?)',(provider,self.fernet.encrypt(key.encode()).decode()))
    def get(self, provider):
        r=self.db.rows('SELECT cipher FROM secrets WHERE provider=?',(provider,))
        if not r: raise ProviderError('missing_key')
        return self.fernet.decrypt(r[0]['cipher'].encode()).decode()

class Provider:
    def __init__(self,vault, transport=None):
        self.vault,self.transport=vault,transport
    async def call(self, model, messages):
        validate_model(model)
        key=self.vault.get(model['provider'])
        body={'model':model['api_model'],'messages':messages,'max_tokens':model.get('max_output_tokens',4096),**model.get('params',{})}
        try:
            async with httpx.AsyncClient(timeout=120,transport=self.transport,follow_redirects=False) as c:
                r=await c.post(model['base_url'].rstrip('/')+'/chat/completions',headers={'Authorization':'Bearer '+key},json=body)
        except httpx.HTTPError:
            raise ProviderError('transport_billing_unknown',uncertain=True) from None
        if r.status_code!=200:
            # Auth/model validation failures are known rejections. 5xx/timeouts may follow acceptance.
            raise ProviderError('http_'+str(r.status_code),uncertain=(r.status_code>=500 or r.status_code==408))
        usage=None
        try:
            data=r.json()
            usage=data.get('usage',{})
            if not isinstance(usage,dict): usage={}
            usage['_reported_model']=data.get('model')
            msg=data['choices'][0]['message']
            if not isinstance(msg,dict) or not isinstance(msg.get('content'),str) or not msg['content']:
                raise ProviderError('empty_response',uncertain=True,usage=usage)
            return msg,usage
        except (KeyError,IndexError,ValueError,TypeError,AttributeError):
            raise ProviderError('invalid_response_billing_unknown',uncertain=True,usage=usage) from None
