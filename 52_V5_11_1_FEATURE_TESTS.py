import importlib.util,json,time
spec=importlib.util.spec_from_file_location('bot','03_rh_chain_bot_v4.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg=json.load(open('01_config.example.json'))
assert cfg['app']['version']=='5.11.1'
assert cfg['early_listing']['enabled'] is True and cfg['early_listing']['auto_demo_buy'] is True
assert cfg['early_listing']['candidate_max_age_minutes']==30
assert cfg['discovery']['early_listing_reserve']==4
assert cfg['discovery']['candidate_pool_per_scan']==160
assert cfg['discovery']['deep_scan_candidates_per_scan']==8
assert cfg['live']['enabled'] is False
assert 'LIVE TRADING' in m.live_trading_report({'live_armed_until':0},cfg)
# Fresh pairs receive an explicit ranking advantage over an otherwise identical mature pair.
api=m.API(cfg)
base={'chainId':'robinhood','marketCap':5000000,'fdv':5000000,'liquidity':{'usd':100000},'volume':{'h24':500000,'m5':3000},'priceChange':{'h1':5,'m5':10},'txns':{'h24':{'buys':120,'sells':100},'m5':{'buys':8,'sells':5}},'pairCreatedAt':int((time.time()-5*60)*1000)}
api.dex_pair=lambda a,c= 'robinhood': base
fresh=api.discovery_rank({'address':'0x'+'1'*40,'chain_id':'robinhood'})['rank']
old=dict(base); old['pairCreatedAt']=int((time.time()-120*60)*1000)
api.dex_pair=lambda a,c='robinhood': old
mature=api.discovery_rank({'address':'0x'+'1'*40,'chain_id':'robinhood'})['rank']
assert fresh>mature, (fresh,mature)
print('V5.11.1 FEATURE TESTS: PASS')


# V5.11.1: Telegram /set changes persist through the public runner's state restore.
import tempfile, pathlib
with tempfile.TemporaryDirectory() as d:
    cp=pathlib.Path(d)/'config.json'; sp=pathlib.Path(d)/'state.json'; cp.write_text(json.dumps(cfg))
    b=m.Bot(str(cp),str(sp))
    assert 'persistent' in b.set_setting('momentum_reserve','3')
    assert b.cfg['discovery']['momentum_reserve']==3
    b2=m.Bot(str(cp),str(sp))
    assert b2.cfg['discovery']['momentum_reserve']==3

# V5.11.1: 4xx provider errors are not retried.
class Resp4xx:
    status_code=422; headers={}
    def json(self): return {}
    def raise_for_status(self): raise RuntimeError('HTTP 422')
class S4xx:
    def __init__(self): self.calls=0
    def get(self,*a,**k): self.calls+=1; return Resp4xx()
a=m.API(cfg); a.s=S4xx()
try: a.get('https://api.blockscout.com/4663/api/v2/tokens/0x'+'1'*40+'/holders',cache_ttl=0); raise AssertionError('expected 422')
except RuntimeError as e: assert '422' in str(e)
assert a.s.calls==1

# V5.11.1: a 1h/5m momentum candidate is eligible for the fresh momentum lane.
mcfg=json.loads(json.dumps(cfg))
item={'name':'MOO-LIKE','address':'0x'+'2'*40,'chain_id':'robinhood'}
b=m.Bot.__new__(m.Bot); b.cfg=mcfg; b.api=type('A',(),{})()
b.api.dex_pair=lambda a,c=None: {'pairCreatedAt':int((time.time()-60*60)*1000),'priceUsd':'1','marketCap':17000000,'liquidity':{'usd':200000},'volume':{'m5':2500},'txns':{'m5':{'buys':8,'sells':6}},'priceChange':{'m5':7,'h1':25},'baseToken':{'symbol':'MOO'}}
e=b.early_listing_signal(item); assert e and e['early_lane']=='MOMENTUM',e
print('V5.11.1 FEATURE TESTS: PASS')
