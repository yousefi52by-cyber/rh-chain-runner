#!/usr/bin/env python3
"""Deterministic tests for the fresh-pool discovery frontier."""
import importlib.util
from pathlib import Path
SRC=Path('03_rh_chain_bot_v4.py'); assert SRC.exists(); compile(SRC.read_text(),str(SRC),'exec')
spec=importlib.util.spec_from_file_location('rhbot',SRC); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
cfg={'scanner':{'timeout_seconds':8,'max_retries':0},'data':{'gecko_rate_limit_per_minute':8,'cache_ttl_seconds':0}}
api=mod.API(cfg); now_iso='2026-09-13T00:00:00Z'; new_addr='So111NEWTESTTOKEN2222222222222222222222222222'; stable_addr='So111STABLETESTTOKEN2222222222222222222222222'
sample={'data':[{'id':'solana_testpool','type':'pool','attributes':{'pool_created_at':now_iso,'reserve_in_usd':'42000','volume_usd':{'h24':'18000'}},'relationships':{'network':{'data':{'id':'solana','type':'network'}},'base_token':{'data':{'id':'solana_new','type':'token'}},'quote_token':{'data':{'id':'solana_usdc','type':'token'}}}},{'id':'solana_stablepool','type':'pool','attributes':{'pool_created_at':now_iso,'reserve_in_usd':'50000'},'relationships':{'network':{'data':{'id':'solana','type':'network'}},'base_token':{'data':{'id':'solana_usdc2','type':'token'}},'quote_token':{'data':{'id':'solana_usdt2','type':'token'}}}}], 'included':[{'id':'solana_new','type':'token','attributes':{'address':new_addr,'name':'NEW TEST','symbol':'NEWT'}},{'id':'solana_usdc','type':'token','attributes':{'address':'USDC','name':'USD Coin','symbol':'USDC'}},{'id':'solana_usdc2','type':'token','attributes':{'address':stable_addr,'name':'USD Coin','symbol':'USDC'}},{'id':'solana_usdt2','type':'token','attributes':{'address':'USDT','name':'Tether','symbol':'USDT'}}]}
def fake_get(url,params=None,cache_ttl=None):
    if url.endswith('/networks/new_pools'): return sample
    if '/token-profiles/' in url or '/token-boosts/' in url: return []
    if url.endswith('/tokens') or url.endswith('/token-transfers'): return {'items':[]}
    raise AssertionError(f'unexpected endpoint: {url}')
api.get=fake_get
items,_=api.discover(10,None); found=[x for x in items if x.get('address')==new_addr]
assert found and found[0].get('chain_id')=='solana' and 'gecko_new_pool' in found[0].get('sources',[]) and found[0].get('new_pool') is True and found[0].get('pair_created_at')
assert not any(x.get('address')==stable_addr for x in items)
api.dex_pair=lambda *args,**kwargs: None
rank=api.discovery_rank(found[0]); assert rank and rank.get('liquidity',0)>0 and rank.get('pair_created_at')==found[0].get('pair_created_at')
print('V5.11.0 EARLY DISCOVERY TESTS OK')
