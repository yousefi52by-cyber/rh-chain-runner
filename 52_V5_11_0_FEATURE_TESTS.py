import importlib.util,json,time
spec=importlib.util.spec_from_file_location('bot','03_rh_chain_bot_v4.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg=json.load(open('01_config.example.json'))
assert cfg['app']['version']=='5.11.0'
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
print('V5.11.0 FEATURE TESTS: PASS')
