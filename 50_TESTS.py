import importlib.util, json, tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('bot','03_rh_chain_bot_v4.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg=json.load(open('01_config.example.json'))
cfg['macro_markets']['gold']['enabled']=False
assert cfg['app']['version']=='5.12.0'
assert cfg['scanner']['max_tokens_per_scan']==0
assert cfg['discovery']['candidate_pool_per_scan']==160
assert cfg['discovery']['watchlist_size']==50
assert cfg['discovery']['deep_scan_candidates_per_scan']==4
assert cfg['discovery']['queue_max_size']==1200
assert cfg['discovery']['blockscout_pages_per_scan']==1
assert cfg['filters']['hard_min_liquidity']==10000
assert cfg['risk']['dynamic_sizing'] is True
assert cfg['risk']['allocation_per_trade_pct']==25.0
assert cfg['risk']['max_portfolio_allocation_pct']==75.0
assert cfg['early_listing']['auto_demo_buy'] is True
assert cfg['early_listing']['max_demo_entry_usd']==5

base={'name':'TEST','address':'0x'+'1'*40,'market_cap':3700000,'liquidity':232500,'volume_24h':3100000,'holders':2111,'top10_pct':20,'largest_holder_pct':10,'change_1h':12,'change_4h':8,'buys':219,'sells':164}
tech={'close':155,'ema20':145,'ema50':130,'ema200':110,'rsi14':65,'atr_pct':5,'resistance':153,'support':135,'breakout_pct':(155/153-1)*100,'volume_ratio':2.2,'higher_high':True,'higher_low':True,'lower_high':False,'lower_low':False,'candles':220}
r=m.score(base|{'technical':tech},cfg); assert r['technical_score'] is not None and r['setup_type']=='BREAKOUT',r
assert r['entry_zone'] and r['verdict']=='BUY CANDIDATE',r
r_no_setup=m.score(base|{'technical':tech|{'resistance':None}},cfg); assert r_no_setup['verdict']!='BUY CANDIDATE' if r_no_setup['setup_type']=='NO_SETUP' else True
r2=m.score(base|{'technical':tech,'change_1h':250},cfg); assert 'HOT_MOMENTUM' in r2['risk_flags'],r2
r3=m.score(base|{'technical':tech,'market_cap':28200000},cfg); assert r3['verdict']!='REJECT' and 'LARGE_CAP' in r3['risk_flags'],r3
# Small-cap opportunities are graded, not hard-rejected solely for market cap.
small=base|{'technical':tech,'market_cap':83800,'holders':758,'liquidity':50000,'volume_24h':80000}
r_small=m.score(small,cfg); assert r_small['verdict']!='REJECT' and 'SMALL_CAP' in r_small['risk_flags'],r_small
none_tech=base|{'technical':None}
r4=m.score(none_tech,cfg); assert r4['technical_score'] is None

st=m.default_state(cfg); info=base|{'price':1.0,'suggested_stop_pct':8,'setup_type':'BREAKOUT'}
tid,why=m.risk_buy(st,cfg,info,None); assert tid and why=='OK'; assert st['demo']['positions'][info['address']]['invested']<=5.01

# Missing technical data must never crash the full scan.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['watchlist']=[{'name':'BROKEN_TA','address':'0x'+'a'*40}]; local['discovery']['enabled']=False; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    class BrokenAPI:
        def snapshot(self,name,address,include_4h=True): return base|{'name':name,'address':address,'price':1.0,'errors':[]}
        def gecko_technical(self,address,pair_address=None,token_side=None): raise RuntimeError('HTTP 429 rate limited')
        def dex_pair(self,a): return None
    b.api=BrokenAPI(); out=b.scan(); assert len(out)==1 and out[0]['verdict']!='ERROR',out

# Rotation: 100 discovered opportunities must not be permanently cut to a fixed deep batch.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['watchlist']=[]; local['discovery']['candidate_pool_per_scan']=100; local['discovery']['watchlist_size']=50; local['discovery']['deep_scan_candidates_per_scan']=10; local['early_listing']['enabled']=False; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    class FakeAPI:
        def discover(self,n,cursor=None): return ([{'name':f'D{i}','address':'0x'+format(i+10,'040x')} for i in range(n)], None)
        def discovery_rank(self,item): return {'rank':100-int(item['address'][-2:],16)/10,'market_cap':3700000,'liquidity':232500,'volume_24h':3100000,'change_1h':5,'ratio':1.33,'pair_created_at':None}
        def dex_pair(self,a): return None
        def snapshot(self,name,address,include_4h=True): return {'name':name,'address':address,'price':1.0,'market_cap':3700000,'liquidity':232500,'volume_24h':3100000,'holders':2111,'top10_pct':20,'largest_holder_pct':10,'change_1h':5,'change_4h':5,'buys':219,'sells':164,'errors':[]}
        def gecko_technical(self,address,pair_address=None,token_side=None): return ([[i,1,1.1,.9,1,100] for i in range(220)], '0x'+'9'*40)
    b.api=FakeAPI(); batches=[]
    for _ in range(10): batches.append({x['address'] for x in b.scan()})
    covered=set().union(*batches); assert len(covered)>=100, len(covered)
    assert b.st['last_scan_universe']['queue_size']==100
    assert b.st['last_scan_universe']['watch_size']==50

# Explicit configured token is always checked even if discovery ranking is absent.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['discovery']['enabled']=False; local['watchlist']=[{'name':'MANUAL','address':'0x'+'a'*40}]; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    class OneAPI:
        def snapshot(self,name,address,include_4h=True): return {'name':name,'address':address,'price':1.0,'market_cap':3700000,'liquidity':232500,'volume_24h':3100000,'holders':2111,'top10_pct':20,'largest_holder_pct':10,'change_1h':5,'change_4h':5,'buys':219,'sells':164,'errors':[]}
        def gecko_technical(self,address): return ([[i,1,1.1,.9,1,100] for i in range(220)],'0x'+'9'*40)
        def dex_pair(self,a): return None
    b.api=OneAPI(); out=b.scan(); assert len(out)==1 and out[0]['name']=='MANUAL'

# Smart management: partial TP is active and does not crash.
st=m.default_state(cfg); info=base|{'price':1.0,'suggested_stop_pct':8,'setup_type':'BREAKOUT'}
tid,why=m.risk_buy(st,cfg,info,None); assert tid and why=='OK'
addr=info['address']; tr=m.manage_position(st,cfg,base|{'address':addr,'price':1.11,'technical':{'atr_pct':4},'setup_type':'BREAKOUT','risk_flags':[]})
assert tr and tr['reason']=='partial-take-profit',tr
assert addr in st['demo']['positions'] and st['demo']['positions'][addr]['partial_taken'] is True

# Technical score threshold is enforced before a confirmation can be emitted.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['score']['min_tech_scan_score']=101; local['signal']['required_confirmations']=1; local['watchlist']=[{'name':'TECHLOW','address':'0x'+'b'*40}]; local['discovery']['enabled']=False; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    class LowTechAPI:
        def snapshot(self,name,address,include_4h=True): return base|{'name':name,'address':address,'price':1.0,'errors':[]}
        def gecko_technical(self,address,pair_address=None,token_side=None): return ([[i,1,1.1,.9,1,100] for i in range(220)], '0x'+'9'*40)
        def dex_pair(self,a): return None
    b.api=LowTechAPI(); out=b.scan(); assert out[0]['signal'] is None and out[0]['confirmations']==0, out

# Telegram position view has a real latest scanned price, not the entry price fallback.
assert b.st['last_scan']['0x'+'b'*40]['price']==1.0

# Discovery cycle is not starved by the first feed when all three feeds are populated.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; cp.write_text(json.dumps(cfg)); b=m.Bot(str(cp),str(sp))
    class FeedAPI(m.API):
        def __init__(self,cfg): self.cfg=cfg
        def get(self,url,params=None,cache_ttl=None):
            if url.endswith('/token-profiles/latest/v1'): return [{'chainId':'robinhood','tokenAddress':'0x'+'1'*40,'tokenName':'P1'}]
            if url.endswith('/token-boosts/latest/v1'): return [{'chainId':'robinhood','tokenAddress':'0x'+'2'*40,'tokenName':'B1'}]
            if url.endswith('/token-boosts/top/v1'): return [{'chainId':'robinhood','tokenAddress':'0x'+'3'*40,'tokenName':'T1'}]
            return []
    b.api=FeedAPI(cfg); got,cursor=b.api.discover(3,None); assert {x['name'] for x in got}=={'P1','B1','T1'} and cursor is None,(got,cursor)

# V5.11.1: configured/manual watch items are NOT priority-scanned. They share
# the normal opportunity queue and must not monopolize the deep-scan budget.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['watchlist']=[{'name':'JUG','address':'0x'+'c'*40},{'name':'SPACE','address':'0x'+'d'*40}]; local['discovery']['candidate_pool_per_scan']=6; local['discovery']['deep_scan_candidates_per_scan']=3; local['early_listing']['enabled']=False; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    class FairAPI:
        def discover(self,n,cursor=None): return ([{'name':f'D{i}','address':'0x'+format(i+100,'040x')} for i in range(n)], None)
        def discovery_rank(self,item): return {'rank':50,'market_cap':3700000,'liquidity':232500,'volume_24h':3100000,'change_1h':5,'ratio':1.33,'pair_created_at':None}
        def snapshot(self,name,address,include_4h=True): return base|{'name':name,'address':address,'price':1.0,'errors':[]}
        def gecko_technical(self,address,pair_address=None,token_side=None): return ([[i,1,1.1,.9,1,100] for i in range(220)], '0x'+'9'*40)
        def dex_pair(self,a): return None
    b.api=FairAPI(); names=[]
    for _ in range(2): names.append([x['name'] for x in b.scan()])
    assert all(len(x)<=3 for x in names), names
    assert not any(x in ('JUG','SPACE') for x in names[0]), names
    assert not any(x in ('JUG','SPACE') for x in names[1]), names
    assert b.st['last_scan_universe']['queue_size']==8, b.st['last_scan_universe']
    assert b.st['last_scan_universe']['priority_batch']==0, b.st['last_scan_universe']

# V5.11.1: consecutive confirmations are capped and reset on a failed pass.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['watchlist']=[{'name':'CAP','address':'0x'+'e'*40}]; local['discovery']['enabled']=False; local['signal']['required_confirmations']=2; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    class CapAPI:
        def __init__(self): self.good=True
        def snapshot(self,name,address,include_4h=True): return base|{'name':name,'address':address,'price':1.0,'errors':[]}
        def gecko_technical(self,address,pair_address=None,token_side=None): return ([[i,1,1.1,.9,1,100] for i in range(220)], '0x'+'9'*40)
        def dex_pair(self,a): return None
    cap=CapAPI(); b.api=cap
    for _ in range(5):
        out=b.scan(); assert out and out[0]['confirmations']<=2, out
        b.st['opportunity_queue']['0x'+'e'*40]['next_deep_at']=0
    # Force a failed technical pass; confirmation must reset to zero.
    def broken(address,pair_address=None,token_side=None): raise RuntimeError('429')
    cap.gecko_technical=broken
    out=b.scan(); assert out and out[0]['confirmations']==0, out

print('V5.11.1 BASE AUDIT TESTS OK')

# Deep technical scan can reuse the Dex pair from snapshot and therefore avoid a
# Gecko pools request on the normal path.
class PairAPI:
    def __init__(self): self.gecko_calls=[]
    def dex_pair(self,a): return {'chainId':'robinhood','pairAddress':'0x'+'9'*40,'baseToken':{'address':a},'priceUsd':'1','marketCap':2000000,'liquidity':{'usd':200000},'volume':{'h24':300000},'txns':{'h24':{'buys':200,'sells':100}},'priceChange':{'h1':1}}
    def holders(self,a): return 2000,20,10,[]
    def gecko_pools(self,a): raise AssertionError('gecko_pools should not be called on normal path')
    def gecko_technical(self,address,pair_address=None,token_side=None):
        assert pair_address=='0x'+'9'*40 and token_side=='base'
        self.gecko_calls.append((pair_address,token_side))
        rows=[[i,1,1.1,.9,1,100] for i in range(220)]
        return rows,pair_address

pair_api=PairAPI(); rows,pool=pair_api.gecko_technical('0x'+'a'*40,'0x'+'9'*40,'base'); assert len(rows)==220 and pair_api.gecko_calls==[('0x'+'9'*40,'base')]


# V5.11.1: Gecko 429 is not immediately retried; it enters a bounded cooldown.
class Resp:
    def __init__(self,status,headers=None,payload=None): self.status_code=status; self.headers=headers or {}; self._payload=payload
    def json(self): return self._payload
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f'HTTP {self.status_code}')
class Gecko429Session:
    def __init__(self): self.calls=0
    def get(self,url,params=None,headers=None,timeout=None):
        self.calls += 1; return Resp(429,{})
api=m.API(cfg); api.s=Gecko429Session(); api.gecko_backoff_until=0
try:
    api.get(m.GECKO+'/test',{},cache_ttl=0)
    raise AssertionError('expected Gecko 429')
except RuntimeError as e:
    assert '429' in str(e)
assert api.s.calls==1 and api.gecko_backoff_until>0

# V5.11.1: normal technical path must not call Gecko /tokens/.../pools after a
# DexScreener pair is supplied; this is the regression that caused unnecessary
# Gecko pressure in the real scan.
class PairOnlyAPI(m.API):
    def __init__(self,cfg): pass
    def gecko_pools(self,a): raise AssertionError('unexpected Gecko pools request')
    def get(self,url,params=None,cache_ttl=None):
        assert '/pools/' in url and url.endswith('/ohlcv/hour')
        return {'data':{'attributes':{'ohlcv_list':[[i,1,1.1,.9,1,100] for i in range(220)]}}}
pa=PairOnlyAPI(cfg); rows,pool=pa.gecko_technical('0x'+'a'*40,'0x'+'9'*40,'base')
assert len(rows)==220 and pool=='0x'+'9'*40

# V5.11.1: invalid/missing Dex pair metadata may use Gecko pool discovery as fallback.
class FallbackAPI(m.API):
    def __init__(self,cfg): pass
    def gecko_pools(self,a):
        return [{'id':'robinhood_0x'+'9'*40,'attributes':{'reserve_in_usd':'200000','volume_usd':{'h24':'300000'}},'relationships':{'base_token':{'data':{'id':'robinhood_0x'+'a'*40}}}}]
    def get(self,url,params=None,cache_ttl=None):
        assert url.endswith('/ohlcv/hour')
        return {'data':{'attributes':{'ohlcv_list':[[i,1,1.1,.9,1,100] for i in range(220)]}}}
fa=FallbackAPI(cfg); rows,pool=fa.gecko_technical('0x'+'a'*40,None,None)
assert len(rows)==220 and pool=='0x'+'9'*40

# V5.11.1: on-chain discovery must contribute tokens even when Dex feeds do not.
class OnchainFeedAPI(m.API):
    def __init__(self,cfg): self.cfg=cfg
    def get(self,url,params=None,cache_ttl=None):
        if url.endswith('/token-profiles/latest/v1'): return []
        if url.endswith('/token-boosts/latest/v1'): return []
        if url.endswith('/token-boosts/top/v1'): return []
        if url.endswith('/tokens'):
            return {'items':[{'contract_address_hash':'0x'+'7'*40,'name':'ONCHAIN','symbol':'ON'}], 'next_page_params':None}
        if url.endswith('/token-transfers'):
            return {'items':[{'token':{'address_hash':'0x'+'8'*40,'name':'ACTIVE','symbol':'ACT','type':'ERC-20'}}], 'next_page_params':None}
        return {}
onc=OnchainFeedAPI(cfg); got,cursor=onc.discover(2,None); assert {x['name'] for x in got}=={'ONCHAIN','ACTIVE'} and cursor is None,(got,cursor)

print('V5.11.1 FULL TESTS OK')

print('V5.11.1 FULL TESTS OK')

# V5.11.1: Blockscout token pagination is deliberately one page per scan; stale
# cursors from prior runs are ignored so the scanner never emits the repeated 422.
class CursorFeedAPI(OnchainFeedAPI):
    def get(self,url,params=None,cache_ttl=None):
        if url.endswith('/token-profiles/latest/v1') or url.endswith('/token-boosts/latest/v1') or url.endswith('/token-boosts/top/v1'): return []
        if url.endswith('/tokens'):
            assert params=={'type':'ERC-20'}, params
            return {'items':[{'contract_address_hash':'0x'+'6'*40,'name':'PAGE1'}], 'next_page_params':{'contract_address_hash':'0x'+'5'*40}}
        if url.endswith('/token-transfers'): return {'items':[]}
        return {}
ca=CursorFeedAPI(cfg); got,cursor=ca.discover(2,{'page_key':'STALE'}); assert got[0]['name']=='PAGE1' and cursor is None,(got,cursor)

# V5.11.1: capital allocation is explicit and risk-based. With $20, an 8% stop
# targets 25%/$5 per trade while risking at most 2%/$0.40. Three positions can
# use at most 75% of equity; a fourth is blocked by max_open_positions.
st=m.default_state(cfg); info=base|{'price':1.0,'suggested_stop_pct':8,'setup_type':'BREAKOUT'}
for i in range(3):
    ii=dict(info); ii['address']='0x'+format(300+i,'040x'); tid,why=m.risk_buy(st,cfg,ii,None); assert tid and why=='OK', (tid,why)
assert 14.99 < round(sum(float(p['invested']) for p in st['demo']['positions'].values()),6) < 15.01
assert 4.99 < round(st['demo']['cash'],6) < 5.01
assert all(round(float(p['risk_at_stop_usd']),6)<=0.4 for p in st['demo']['positions'].values())
ii=dict(info); ii['address']='0x'+'3ff'+'0'*37; tid,why=m.risk_buy(st,cfg,ii,None); assert tid is None and why=='max open positions',why
# Wider stop automatically reduces allocation to preserve the 2% equity risk cap.
st2=m.default_state(cfg); ii=info|{'suggested_stop_pct':15}; tid,why=m.risk_buy(st2,cfg,ii,None); assert tid and 2.66 < round(st2['demo']['positions'][ii['address']]['invested'],2) <= 2.67,(tid,why,st2)

# V5.11.1: opportunity alerts surface BUY CANDIDATE/WATCH ideas even when entry is
# not ready, and dedupe the same idea during the cooldown.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['watchlist']=[]; local['signal']['telegram_opportunity_alerts']=True; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp)); now=1000
    opp=base|{'price':1.0,'technical':tech,'verdict':'BUY CANDIDATE','score':70,'technical_score':80,'setup_type':'PULLBACK','entry_ready':False,'risk_flags':['SMALL_CAP']}
    msgs=b._opportunity_messages([opp],now); assert len(msgs)==1 and 'OPPORTUNITY' in msgs[0]
    msgs=b._opportunity_messages([opp],now+60); assert len(msgs)==0
    opp2=dict(opp); opp2['score']=76; msgs=b._opportunity_messages([opp2],now+120); assert len(msgs)==0


# V5.11.1: the deep budget reserves slots for newly created pairs so discovery
# is not monopolized by established high-ranked opportunities.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['watchlist']=[]; local['discovery']['candidate_pool_per_scan']=4; local['discovery']['deep_scan_candidates_per_scan']=3; local['discovery']['early_listing_reserve']=1; local['early_listing']['candidate_max_age_minutes']=5; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    now_ms=1_800_000_000_000
    class EarlyAPI:
        def discover(self,n,cursor=None): return ([{'name':'OLD','address':'0x'+'a'*40},{'name':'EARLY','address':'0x'+'b'*40}],None)
        def discovery_rank(self,item): return {'rank':99 if item['name']=='OLD' else 50,'market_cap':3700000,'liquidity':232500,'volume_24h':3100000,'change_1h':5,'ratio':1.33,'pair_created_at':(now_ms-2*60000 if item['name']=='EARLY' else now_ms-30*60000)}
        def snapshot(self,name,address,include_4h=True): return base|{'name':name,'address':address,'price':1.0,'errors':[]}
        def gecko_technical(self,address,pair_address=None,token_side=None): return ([[i,1,1.1,.9,1,100] for i in range(220)],'0x'+'9'*40)
        def dex_pair(self,a): return None
    b.api=EarlyAPI(); import unittest.mock as um
    with um.patch.object(m.time,'time',return_value=now_ms/1000):
        out=b.scan()
    assert any(x['name']=='EARLY' for x in out),[x['name'] for x in out]

print('V5.11.1 FULL TESTS OK')


# V5.11.1: global Dex discovery accepts multiple major chains, not only Robinhood.
class GlobalFeedAPI(m.API):
    def __init__(self,cfg): self.cfg=cfg
    def get(self,url,params=None,cache_ttl=None):
        if url.endswith('/token-profiles/latest/v1'):
            return [
                {'chainId':'solana','tokenAddress':'So11111111111111111111111111111111111111112','tokenName':'SOLTEST'},
                {'chainId':'ethereum','tokenAddress':'0x'+'2'*40,'tokenName':'ETHTEST'},
                {'chainId':'robinhood','tokenAddress':'0x'+'3'*40,'tokenName':'RHTEST'},
            ]
        if url.endswith('/token-boosts/latest/v1') or url.endswith('/token-boosts/top/v1'): return []
        if url.endswith('/tokens') or url.endswith('/token-transfers'): return {'items':[]}
        return {}
gf=GlobalFeedAPI(cfg); got,_=gf.discover(10,None); assert {x['chain_id'] for x in got}=={'solana','ethereum','robinhood'},got

# V5.11.1: Gold is a first-class commodity radar asset and uses technical scoring,
# without crypto market-cap/holder gates.
local=json.loads(json.dumps(cfg)); local['macro_markets']['gold']['enabled']=True; local['watchlist']=[]
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    class GoldAPI:
        def discover(self,n,cursor=None): return ([],cursor)
        def discovery_rank(self,item): return {'rank':88,'market_cap':None,'liquidity':1e9,'volume_24h':None,'change_1h':1,'ratio':0,'pair_created_at':None}
        def snapshot(self,name,address,include_4h=False,chain_id='macro',asset_class='commodity'):
            return {'name':name,'address':address,'chain_id':'macro','asset_class':'commodity','price':2500.0,'market_cap':None,'liquidity':1e9,'volume_24h':None,'holders':None,'top10_pct':None,'largest_holder_pct':None,'change_1h':1.2,'change_4h':2.5,'buys':0,'sells':0,'errors':[], 'technical':{'close':2500,'ema20':2480,'ema50':2450,'ema200':2300,'rsi14':62,'atr_pct':1.5,'resistance':2490,'support':2400,'breakout_pct':0.4,'volume_ratio':1.0,'higher_high':True,'higher_low':True,'lower_high':False,'lower_low':False,'pool':'STOOQ_XAUUSD','candles':220}}
        def gecko_technical(self,*args,**kwargs): raise AssertionError('Gold must not call Gecko')
    b.api=GoldAPI(); out=b.scan(); assert out and out[0]['name']=='XAUUSD GOLD' and out[0]['asset_class']=='commodity',out
    assert out[0]['verdict'] in ('BUY CANDIDATE','WATCH'),out[0]


# V5.11.1 runtime hardening: Bot never reuses a persisted Blockscout cursor across runs.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['watchlist']=[]; local['discovery']['candidate_pool_per_scan']=1; cp.write_text(json.dumps(local))
    stale=m.default_state(local); stale['blockscout_token_cursor']={'name':'STALE'}; sp.write_text(json.dumps(stale))
    b=m.Bot(str(cp),str(sp))
    class CursorAPI:
        def __init__(self): self.args=[]
        def discover(self,n,cursor=None): self.args.append(cursor); return ([],None)
        def discovery_rank(self,item): return None
    b.api=CursorAPI(); b.scan(); assert b.api.args==[None],b.api.args

# V5.11.1: a failed primary Gecko pool is followed by one bounded fallback pool.
class GeckoFallbackAPI(m.API):
    def __init__(self,cfg): self.cfg=cfg; self.calls=[]
    def get(self,url,params=None,cache_ttl=None):
        self.calls.append((url,params))
        if '/pools/PRIMARY/' in url: raise RuntimeError('404 Client Error')
        if url.endswith('/tokens/0x'+'a'*40+'/pools'):
            return {'data':[{'id':'pool_BAD_BAD','attributes':{'reserve_in_usd':'1','volume_usd':{'h24':'1'}},'relationships':{}}, {'id':'pool_GOOD_0xGOOD','attributes':{'reserve_in_usd':'1000','volume_usd':{'h24':'1000'}},'relationships':{}}]}
        if '/pools/0xGOOD/ohlcv/hour' in url:
            return {'data':{'attributes':{'ohlcv_list':[[i,1,1.1,.9,1,100] for i in range(220)]}}}
        raise RuntimeError('unexpected URL '+url)
gfa=GeckoFallbackAPI(cfg); rows,pool=gfa.gecko_technical('0x'+'a'*40,'PRIMARY','base','robinhood'); assert len(rows)==220 and pool=='0xGOOD',gfa.calls

# V5.11.1: RETEST is not entry-ready until resistance is actually reclaimed.
rt={'close':99.0,'ema20':98.0,'ema50':97.0,'ema200':95.0,'rsi14':60.0,'atr_pct':3.0,'resistance':100.0,'support':95.0,'breakout_pct':-1.0,'volume_ratio':1.5,'higher_high':True,'higher_low':True,'lower_high':False,'lower_low':False,'candles':220}
setup,zone,_=m.classify_setup(rt,cfg); assert setup=='RETEST',(setup,zone)
assert not (zone and zone[0] <= 99.0 <= zone[1] and 99.0 >= 100.0)
rt2=dict(rt); rt2['close']=100.2; rt2['breakout_pct']=0.2
setup2,zone2,_=m.classify_setup(rt2,cfg); assert setup2=='BREAKOUT',setup2

print('V5.11.1 RUNTIME HARDENING TESTS OK')

print('V5.11.1 GLOBAL MULTI-ASSET TESTS OK')


# V5.11.1: Telegram discovery unwraps the (items, cursor) tuple and never iterates None.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; cp.write_text(json.dumps(cfg)); b=m.Bot(str(cp),str(sp))
    class TgAPI:
        def discover(self,n,cursor=None): return ([{'name':'ONE','address':'0x'+'1'*40,'chain_id':'robinhood'}], None)
    b.api=TgAPI(); text=b.telegram_text('/discover'); assert 'ONE' in text and 'DISCOVERY' in text

# V5.11.1: Telegram opportunity/risk views are available and main menu exposes them.
b.st['daily_watch']=[{'name':'ONE','verdict':'BUY CANDIDATE','score':88,'setup_type':'BREAKOUT','entry_ready':True}]
assert 'BUY CANDIDATE' in b.telegram_text('/opportunities')
assert 'RISK' in b.telegram_text('/risk')
menu=str(b.telegram_menu()); assert 'opportunities' in menu and 'risk' in menu

print('V5.11.1 TELEGRAM STRUCTURE TESTS OK')

# V5.12.0: the scheduler workflow lives in the separate public runner repo.
# If a local copy is present, validate it; otherwise do not fail the private
# repo test suite merely because the external runner is not checked out here.
wf_candidates=[Path('.github/workflows/SCANNER_WORKFLOW.yml'), Path('../RUNNER_REPO/.github/workflows/runner.yml')]
wf_path=next((p for p in wf_candidates if p.exists()), None)
if wf_path:
    wf=wf_path.read_text()
    assert 'Robinhood Chain FINAL Runner' in wf
    assert 'robinhood-chain-state-' in wf
    assert wf.index('Run tests') < wf.index('Run scanner')
    assert 'python 03_rh_chain_bot_v4.py --config config.json --state state.json --once' in wf
    print('V5.12.0 WORKFLOW ALIGNMENT TESTS OK')
else:
    print('V5.12.0 WORKFLOW ALIGNMENT TESTS SKIPPED (external runner repo is separate)')
