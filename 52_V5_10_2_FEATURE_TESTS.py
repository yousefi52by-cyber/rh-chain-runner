import importlib.util,json
spec=importlib.util.spec_from_file_location('bot','03_rh_chain_bot_v4.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
cfg=json.load(open('01_config.example.json'))
assert cfg['app']['version']=='5.10.2'
assert cfg['early_listing']['enabled'] is True and cfg['early_listing']['auto_demo_buy'] is True
assert cfg['live']['enabled'] is False
st={'demo':{'trades':[],'cash':20,'positions':{},'realized_pnl':0},'early_listing_watch':[{'name':'TEST','pair_age_minutes':2,'change_5m':15,'buys_5m':10,'sells_5m':5,'liquidity':30000}]}
assert 'TEST' in m.early_listing_report(st)
print('V5.10.2 FEATURE TESTS: PASS')
