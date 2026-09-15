#!/usr/bin/env python3
"""V5.12.0 FINAL completion/regression tests.
Covers the final standalone source, provider hardening, demo accounting and Telegram surface.
"""
import importlib.util, json, tempfile
from pathlib import Path

SRC=Path('03_rh_chain_bot_v4.py'); assert SRC.exists()
src=SRC.read_text(); compile(src,str(SRC),'exec')
assert 'raw.githubusercontent.com' not in src, 'FINAL source must be standalone'
assert 'V5.12.0 FINAL' in src
spec=importlib.util.spec_from_file_location('rhbot',SRC); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg=json.loads(Path('01_config.example.json').read_text())
assert cfg['app']['version']=='5.12.0'
assert cfg['scanner']['interval_seconds']==600
assert cfg['discovery']['deep_scan_candidates_per_scan']==4
assert cfg['discovery']['early_listing_reserve']==4
assert cfg['discovery']['momentum_reserve']==2
assert cfg['data']['gecko_rate_limit_per_minute']==8
assert cfg['data']['cache_ttl_seconds']==300
assert cfg['risk']['demo_fee_pct']==0.10 and cfg['risk']['demo_slippage_pct']==0.15
assert cfg['live']['enabled'] is False
assert cfg['security']['never_store_private_key'] is True

# Standalone state migration keeps old cached state compatible.
st=m.default_state(cfg); assert st['version']=='5.12.0' and st['state_schema']=='5.12.0'

# Demo fees/slippage are charged and mark-to-market equity follows the latest price.
base={'name':'TEST','address':'0x'+'1'*40,'price':1.0,'score':80,'setup_type':'BREAKOUT','suggested_stop_pct':8}
tid,why=m.risk_buy(st,cfg,base,5.0); assert tid and why=='OK'
addr=base['address'].lower(); pos=st['demo']['positions'][addr]
assert pos['entry']>1.0 and pos['entry_fee']>0 and pos['invested']>5.0
cash_after=st['demo']['cash']; pos['last_price']=1.10
assert m._portfolio_equity(st)>cash_after+pos['invested']
tr,_=m.risk_sell(st,cfg,addr,1.10,'test-exit'); assert tr and tr['exit']<1.10 and tr['exit_fee']>0

# Non-EVM addresses must not call Blockscout holders.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; cp.write_text(json.dumps(cfg))
    b=m.Bot(str(cp),str(sp))
    class A(m.API):
        def __init__(self): pass
        def dex_pair(self,*a,**k): return {'priceUsd':'1','marketCap':'1000000','liquidity':{'usd':'20000'},'volume':{'h24':'50000'},'txns':{'h24':{'buys':10,'sells':5}},'priceChange':{'h1':5},'pairAddress':'P','baseToken':{'address':a[1] if len(a)>1 else a[0]}}
        def gecko_pools(self,*a,**k): return []
        def holders(self,*a,**k): raise AssertionError('holders must be skipped for non-EVM address')
    b.api=A()
    s=b.api.snapshot('SOL','So11111111111111111111111111111111111111111',include_4h=False,chain_id='solana')
    assert s['chain_id']=='solana'

# Telegram surface contains all core controls and LIVE remains locked.
b=m.Bot.__new__(m.Bot); b.cfg=cfg; b.st=st
menu=b.telegram_menu(); labels=[x['text'] for row in menu['inline_keyboard'] for x in row]
for required in ['📊 اسکن بازار','🎯 فرصت‌ها','🔎 بررسی توکن','🔍 Discovery','💰 Demo','📦 پوزیشن‌ها','👀 Watchlist','📈 گزارش‌ها','🛡 ریسک','⚙️ تنظیمات','🔐 Live','🛑 PANIC STOP']:
    assert required in labels, required
assert 'LOCKED' in m.live_trading_report(st,cfg)

# Global multi-asset regression: non-Robinhood tokens do not require the
# Robinhood-only Blockscout holder fields in order to become opportunities.
non_rh={
    'name':'SOLTEST','address':'So11111111111111111111111111111111111111111',
    'chain_id':'solana','asset_class':'crypto','market_cap':3700000,
    'liquidity':232500,'volume_24h':3100000,'holders':None,'top10_pct':None,
    'largest_holder_pct':None,'change_1h':12,'change_4h':8,'buys':219,'sells':164,
    'technical':{'close':155,'ema20':145,'ema50':130,'ema200':110,'rsi14':65,
                 'atr_pct':5,'resistance':153,'support':135,'breakout_pct':(155/153-1)*100,
                 'volume_ratio':2.2,'higher_high':True,'higher_low':True,
                 'lower_high':False,'lower_low':False,'candles':220}
}
r=m.score(non_rh,cfg); assert r['verdict']!='DATA_INCOMPLETE', r

# Demo positions retain their chain/asset class so mandatory position-management
# scans do not accidentally query Robinhood Chain for a position from another chain.
st=m.default_state(cfg); info={**non_rh,'price':1.0,'score':80,'setup_type':'BREAKOUT','suggested_stop_pct':8}
tid,why=m.risk_buy(st,cfg,info,5.0); assert tid and why=='OK'
pos=st['demo']['positions'][info['address'].lower()]
assert pos['chain_id']=='solana' and pos['asset_class']=='crypto'

# Gold is observation-only by default; a confirmed signal must not auto-open a
# Demo trade when macro_markets.gold.auto_demo_buy is false.
with tempfile.TemporaryDirectory() as d:
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; local=json.loads(json.dumps(cfg)); local['telegram']['enabled']=False; local['watchlist']=[]; cp.write_text(json.dumps(local))
    b=m.Bot(str(cp),str(sp))
    class GoldAPI:
        def discover(self,n,cursor=None): return [],None
        def discovery_rank(self,item): return None
        def snapshot(self,name,address,include_4h=True,**kwargs):
            return {'name':name,'address':address,'chain_id':'macro','asset_class':'commodity','price':3500,
                    'market_cap':None,'liquidity':1e9,'volume_24h':None,'holders':None,'top10_pct':None,
                    'largest_holder_pct':None,'change_1h':1,'change_4h':4,'buys':0,'sells':0,'errors':[],
                    'technical':{'close':3500,'ema20':3400,'ema50':3300,'ema200':3000,'rsi14':60,
                                 'atr_pct':2,'resistance':3450,'support':3300,'breakout_pct':1.45,
                                 'volume_ratio':1.5,'higher_high':True,'higher_low':True,
                                 'lower_high':False,'lower_low':False,'candles':300}}
    b.api=GoldAPI(); out=b.scan(); assert out and out[0]['verdict']=='BUY CANDIDATE', out; assert not b.st['demo']['positions'], b.st['demo']['positions']

print('V5.12.0 FINAL COMPLETION TESTS: PASS')
