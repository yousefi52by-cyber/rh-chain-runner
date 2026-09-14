#!/usr/bin/env python3
"""Global Multi-Asset Trader V5.10.1. Opportunity-first scanner/demo/Telegram controller.
Live trading is intentionally locked; no private key is accepted or stored.
"""
import json, os, re, time, uuid, logging, threading, base64, hashlib, hmac, struct, math
from collections import deque
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import datetime, timezone, timedelta
import requests

DEX='https://api.dexscreener.com'
GECKO='https://api.geckoterminal.com/api/v2'
BLOCKSCOUT='https://api.blockscout.com/4663/api/v2'
TG='https://api.telegram.org/bot{}/{}'
CHAIN='robinhood'; CHAIN_ID=4663
UA='Global-Trader-V5.10.1/1.0'
# DexScreener chain IDs -> GeckoTerminal network slugs for deep technical OHLCV.
GECKO_NETWORKS={
    'ethereum':'eth','solana':'solana','bsc':'bsc','base':'base','arbitrum':'arbitrum','polygon':'polygon_pos',
    'optimism':'optimism','avalanche':'avax','robinhood':'robinhood','linea':'linea','blast':'blast',
    'zksync':'zksync_era','scroll':'scroll','mantle':'mantle','cronos':'cronos','sui':'sui-network'
}
GLOBAL_CHAIN_ALLOWLIST=set(GECKO_NETWORKS)
STOOQ_XAU='https://stooq.com/q/d/l/?s=xauusd&i=d'

GECKO_ACCEPT='application/json;version=20230203'
ADDR_RE=re.compile(r'^0x[a-fA-F0-9]{40}$')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('rhbot')


def load_json(path, default):
    p=Path(path)
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except Exception: return default

def atomic_json(path, obj):
    p=Path(path); tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2))
    tmp.replace(p)

def default_state(cfg):
    d=datetime.now(timezone.utc).date().isoformat()
    return {'version':'5.10.2','state_schema':'5.10.2','panic':False,'live_armed_until':0,'confirmations':{},'last_alert':{},'signal_active':{},'signal_last_score':{},'signal_setup':{},
      'watchlist':[],'manual_watchlist':[],'daily_watch':[],'daily_watch_date':d,'telegram_offset':0,'last_scan_universe':{},'entry_cooldowns':{},
      'opportunity_queue':{},'deep_cursor':0,'priority_cursor':0,'blockscout_token_cursor':None,'scan_cycle':0,'opportunity_alerts':{},'last_opportunity_digest':0,
      'demo':{'cash':float(cfg['risk']['demo_start_balance_usd']),'positions':{},'realized_pnl':0.0,'trades':[]},
      'today':{'date':d,'loss':0.0,'trades':0}}

def rollover(st):
    d=datetime.now(timezone.utc).date().isoformat()
    if st.get('today',{}).get('date')!=d: st['today']={'date':d,'loss':0.0,'trades':0}
    if st.get('daily_watch_date')!=d: st['daily_watch']=[]; st['daily_watch_date']=d; st['deep_cursor']=0

class API:
    def __init__(self,cfg):
        self.cfg=cfg
        self.timeout=min(8.0, max(3.0, float(cfg['scanner'].get('timeout_seconds',8))))
        self.retries=min(1, max(0, int(cfg['scanner'].get('max_retries',1))))
        self.s=requests.Session()
        self.s.headers.update({'User-Agent':UA,'Accept':'application/json'})
        self.blockscout_key=os.getenv('BLOCKSCOUT_API_KEY','').strip()
        self.gecko_limit=max(1,min(20,int(cfg.get('data',{}).get('gecko_rate_limit_per_minute',20))))
        self.gecko_calls=deque()
        self.gecko_backoff_until=0.0
        self.cache={}
        self.cache_ttl=float(cfg.get('data',{}).get('cache_ttl_seconds',60))
        self.lock=threading.Lock()

    def _is_gecko(self,url):
        return url.startswith(GECKO)

    def _cache_key(self,url,params):
        return url+'?'+('&'.join(f'{k}={params[k]}' for k in sorted(params or {})))

    def _gecko_wait(self):
        with self.lock:
            now=time.time()
            if now < self.gecko_backoff_until:
                wait=self.gecko_backoff_until-now
                log.info('Gecko backoff: sleeping %.1fs',wait)
                time.sleep(wait)
                now=time.time()
            while self.gecko_calls and now-self.gecko_calls[0]>=60:
                self.gecko_calls.popleft()
            if len(self.gecko_calls)>=self.gecko_limit:
                wait=max(0.05,60-(now-self.gecko_calls[0])+0.05)
                log.info('Gecko rate guard: sleeping %.1fs',wait)
                time.sleep(wait)
                now=time.time()
                while self.gecko_calls and now-self.gecko_calls[0]>=60:
                    self.gecko_calls.popleft()
            self.gecko_calls.append(time.time())

    def _gecko_backoff(self, retry_after=0.0):
        # Some Gecko 429 responses omit Retry-After. Use a bounded local cooldown
        # rather than immediately retrying into the same server-side rate window.
        delay=max(2.0,min(15.0,float(retry_after or 0.0))) if retry_after else 8.0
        with self.lock:
            self.gecko_backoff_until=max(self.gecko_backoff_until,time.time()+delay)
        return delay

    def get(self,url,params=None,cache_ttl=None):
        key=self._cache_key(url,params)
        ttl=self.cache_ttl if cache_ttl is None else cache_ttl
        now=time.time()
        if ttl>0:
            hit=self.cache.get(key)
            if hit and now-hit[0]<ttl:
                return hit[1]
        last=None
        for i in range(self.retries+1):
            try:
                if self._is_gecko(url): self._gecko_wait()
                headers={'Accept':GECKO_ACCEPT} if self._is_gecko(url) else {}
                req_params=dict(params or {})
                if url.startswith('https://api.blockscout.com/') and self.blockscout_key:
                    # Pro API documents the API key as a required query parameter.
                    # Keep Bearer auth too for compatibility with endpoints accepting it.
                    headers['Authorization']=f'Bearer {self.blockscout_key}'
                    req_params.setdefault('apikey', self.blockscout_key)
                r=self.s.get(url,params=req_params,headers=headers,timeout=self.timeout)
                if r.status_code==429:
                    retry_after=float(r.headers.get('Retry-After') or 0)
                    if self._is_gecko(url):
                        # Do not immediately retry Gecko 429s: repeated retries can
                        # extend the provider-side rate window and waste the scan budget.
                        delay=self._gecko_backoff(retry_after)
                        raise RuntimeError(f'HTTP 429 rate limited; local cooldown={delay:.0f}s')
                    if i<self.retries:
                        delay=min(10.0,retry_after or 2.0)
                        log.warning('HTTP 429 on API; retrying after %.1fs', delay)
                        time.sleep(delay)
                        continue
                    raise RuntimeError(f'HTTP 429 rate limited{f"; retry-after={retry_after:.0f}s" if retry_after else ""}')
                if r.status_code in (401,403):
                    raise RuntimeError(f'HTTP {r.status_code} unauthorized/forbidden')
                r.raise_for_status(); data=r.json()
                if ttl>0: self.cache[key]=(time.time(),data)
                return data
            except Exception as e:
                last=e
                if self._is_gecko(url) and '429' in str(e):
                    raise
                if i<self.retries:
                    time.sleep(min(3.0,0.75*(i+1)))
        raise last

    def dex_pair(self,a,chain_id=CHAIN):
        chain=str(chain_id or CHAIN)
        x=self.get(f'{DEX}/token-pairs/v1/{chain}/{a}',cache_ttl=20)
        ps=[p for p in (x or []) if str(p.get('chainId'))==chain]
        return max(ps,key=lambda p:float((p.get('liquidity') or {}).get('usd') or 0)) if ps else None

    def gecko_pools(self,a,chain_id=CHAIN):
        network=GECKO_NETWORKS.get(str(chain_id),str(chain_id))
        x=self.get(f'{GECKO}/networks/{network}/tokens/{a}/pools',{'include':'base_token,quote_token'},cache_ttl=60)
        return x.get('data',[]) if isinstance(x,dict) else []

    @staticmethod
    def _pool_token_side(pool,address):
        rel=((pool.get('relationships') or {}))
        target=address.lower()
        for side in ('base_token','quote_token'):
            link=rel.get(side) or {}
            data=link.get('data') if isinstance(link,dict) else None
            ident=data.get('id','') if isinstance(data,dict) else ''
            if str(ident).lower().endswith(target): return 'base' if side=='base_token' else 'quote'
        attrs=pool.get('attributes') or {}
        for side in ('base_token_address','quote_token_address'):
            if str(attrs.get(side,'')).lower()==target:
                return 'base' if side.startswith('base_') else 'quote'
        return None

    def gecko_4h(self,a,chain_id=CHAIN):
        network=GECKO_NETWORKS.get(str(chain_id),str(chain_id))
        try: pools=self.gecko_pools(a,chain_id)
        except TypeError: pools=self.gecko_pools(a)
        if not pools: raise RuntimeError('no Gecko pool')
        def rank(pool):
            attrs=pool.get('attributes') or {}
            reserve=float(attrs.get('reserve_in_usd') or 0)
            vol=attrs.get('volume_usd') or {}
            v24=float(vol.get('h24') or 0) if isinstance(vol,dict) else 0
            return reserve*0.75+v24*0.25
        ranked=sorted(pools,key=rank,reverse=True)
        for pool in ranked[:3]:
            pid=str(pool.get('id','')); pa=pid.rsplit('_',1)[-1]
            if not str(pa).strip(): continue
            side=self._pool_token_side(pool,a) or 'base'
            try:
                x=self.get(f'{GECKO}/networks/{network}/pools/{pa}/ohlcv/hour',
                           {'aggregate':1,'limit':8,'currency':'usd','token':side},cache_ttl=60)
                rows=(((x.get('data') or {}).get('attributes') or {}).get('ohlcv_list') or [])
                rows=[r for r in rows if isinstance(r,(list,tuple)) and len(r)>=5]
                rows.sort(key=lambda r:float(r[0]),reverse=True)
                if len(rows)>=5:
                    latest=float(rows[0][4]); old=float(rows[4][4])
                    if old>0: return (latest/old-1)*100,pa
            except Exception as e:
                log.warning('Gecko pool %s failed: %s',pa,e)
        raise RuntimeError('insufficient Gecko OHLCV')

    def gecko_technical(self,a,pair_address=None,token_side=None,chain_id=CHAIN):
        """Get bounded technical OHLCV.
        Use the DexScreener pair first. If that exact Gecko pool is genuinely 404,
        make one best-pool discovery and one fallback OHLCV request. Do not retry
        429s and do not fan out across multiple pools.
        """
        network=GECKO_NETWORKS.get(str(chain_id),str(chain_id))
        candidates=[]; seen=set()
        if pair_address and str(pair_address).strip():
            candidates.append((str(pair_address), token_side or 'base')); seen.add(str(pair_address).lower())
        def add_best_pool():
            try:
                try: pools=self.gecko_pools(a,chain_id)
                except TypeError: pools=self.gecko_pools(a)
            except Exception as e:
                raise RuntimeError(f'Gecko pool discovery failed: {e}')
            if not pools: raise RuntimeError('no Gecko pool')
            def rank(pool):
                attrs=pool.get('attributes') or {}
                reserve=float(attrs.get('reserve_in_usd') or 0)
                vol=attrs.get('volume_usd') or {}
                v24=float(vol.get('h24') or 0) if isinstance(vol,dict) else 0
                return reserve*0.75+v24*0.25
            for pool in sorted(pools,key=rank,reverse=True):
                pid=str(pool.get('id','')); pa=pid.rsplit('_',1)[-1]
                if pa and pa.lower() not in seen:
                    candidates.append((pa,self._pool_token_side(pool,a) or 'base')); seen.add(pa.lower()); return
            raise RuntimeError('no valid Gecko pool')
        if not candidates: add_best_pool()
        pa,side=candidates[0]
        try:
            x=self.get(f'{GECKO}/networks/{network}/pools/{pa}/ohlcv/hour',
                       {'aggregate':1,'limit':220,'currency':'usd','token':side},cache_ttl=60)
            rows=(((x.get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []) if isinstance(x,dict) else []
            rows=[r for r in rows if isinstance(r,(list,tuple)) and len(r)>=6]
            rows.sort(key=lambda r:float(r[0]))
            if len(rows)>=200: return rows,pa
            raise RuntimeError(f'insufficient Gecko technical OHLCV ({len(rows)})')
        except RuntimeError as e:
            if pair_address and '404' in str(e):
                add_best_pool()
                pa,side=candidates[-1]
                x=self.get(f'{GECKO}/networks/{network}/pools/{pa}/ohlcv/hour',
                           {'aggregate':1,'limit':220,'currency':'usd','token':side},cache_ttl=60)
                rows=(((x.get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []) if isinstance(x,dict) else []
                rows=[r for r in rows if isinstance(r,(list,tuple)) and len(r)>=6]
                rows.sort(key=lambda r:float(r[0]))
                if len(rows)>=200: return rows,pa
                raise RuntimeError(f'insufficient Gecko technical OHLCV ({len(rows)})')
            raise
    @staticmethod
    def _dec(v):
        try:return Decimal(str(v))
        except (InvalidOperation,TypeError,ValueError):return Decimal('0')

    def holders(self,a):
        """Return holder count, top10 %, largest-holder %, plus data errors.
        Uses the current Blockscout Pro API for Robinhood Chain (chain 4663).
        """
        holders=None; top10=None; largest=None; errors=[]; total=Decimal('0')
        if not self.blockscout_key:
            errors.append('blockscout_api_key_missing')
            errors.extend(['holders_unavailable','top10_unavailable','largest_holder_unavailable'])
            return holders,top10,largest,errors

        # 1) Token info: holders_count + total_supply from the current Blockscout PRO API.
        try:
            tok=self.get(f'{BLOCKSCOUT}/tokens/{a}',cache_ttl=300)
            obj=(tok.get('token') if isinstance(tok,dict) and isinstance(tok.get('token'),dict) else tok)
            if isinstance(obj,dict):
                raw_h=obj.get('holders_count') or obj.get('holdersCount')
                if raw_h is not None: holders=int(raw_h)
                total=self._dec(obj.get('total_supply') or obj.get('totalSupply'))
        except Exception as e:
            errors.append('blockscout_token:'+str(e))

        # 2) Holder balances. REST endpoint first.
        items=[]
        try:
            hdata=self.get(f'{BLOCKSCOUT}/tokens/{a}/holders',cache_ttl=300)
            items=hdata.get('items',[]) if isinstance(hdata,dict) else []
        except Exception as e:
            errors.append('blockscout_holders:'+str(e))

        vals=[]
        pcts=[]
        for item in items:
            if not isinstance(item,dict): continue
            v=self._dec(item.get('value') if item.get('value') is not None else item.get('balance'))
            if v>0: vals.append(v)
            pv=item.get('percentage')
            try:
                if pv is not None: pcts.append(float(pv))
            except Exception: pass
        vals.sort(reverse=True)
        if total>0 and vals:
            top10=float((sum(vals[:10],Decimal('0'))/total)*Decimal('100'))
            largest=float((vals[0]/total)*Decimal('100'))
        elif pcts:
            pcts.sort(reverse=True)
            largest=pcts[0]
            top10=sum(pcts[:10])

        if holders is None: errors.append('holders_unavailable')
        if top10 is None: errors.append('top10_unavailable')
        if largest is None: errors.append('largest_holder_unavailable')
        return holders,top10,largest,errors

    def snapshot(self,name,a,include_4h=True,chain_id=CHAIN,asset_class='crypto'):
        s={'name':name.upper(),'address':a,'ts':time.time(),'errors':[],'chain_id':chain_id,'asset_class':asset_class}
        if asset_class=='commodity' and str(a).upper()=='XAUUSD':
            return self.macro_snapshot_xau(s,include_4h)
        try:
            p=self.dex_pair(a,chain_id)
            if not p: raise RuntimeError('no DexScreener pair')
            s.update(price=float(p.get('priceUsd') or 0),market_cap=float(p.get('marketCap') or p.get('fdv') or 0),liquidity=float((p.get('liquidity') or {}).get('usd') or 0),volume_24h=float((p.get('volume') or {}).get('h24') or 0))
            tx=p.get('txns',{}).get('h24',{}); s.update(buys=int(tx.get('buys') or 0),sells=int(tx.get('sells') or 0))
            pc=p.get('priceChange') or {}; s['change_1h']=float(pc['h1']) if pc.get('h1') is not None else None
            pair_address=p.get('pairAddress')
            base_address=((p.get('baseToken') or {}).get('address') or '')
            s['pair_address']=pair_address if str(pair_address or '').strip() else None
            s['token_side']='base' if str(base_address).lower()==str(a).lower() else 'quote'
        except Exception as e: s['errors'].append('dex:'+str(e))
        if include_4h:
            try:s['change_4h'],s['pool']=self.gecko_4h(a,chain_id)
            except Exception as e:s['change_4h']=None;s['errors'].append('gecko_4h:'+str(e))
        else:s['change_4h']=None
        if str(chain_id)=='robinhood':
            h,t,l,he=self.holders(a); s.update(holders=h,top10_pct=t,largest_holder_pct=l); s['errors'].extend(he)
        else:
            s.update(holders=None,top10_pct=None,largest_holder_pct=None)
        return s

    def macro_snapshot_xau(self,s,include_4h=True):
        try:
            r=self.s.get(STOOQ_XAU,timeout=self.timeout); r.raise_for_status(); text=r.text
            lines=[x.strip() for x in text.splitlines() if x.strip()]
            if len(lines)<30: raise RuntimeError('insufficient XAUUSD history')
            rows=[]
            for line in lines[1:]:
                parts=line.split(',')
                if len(parts)>=5:
                    try: rows.append((parts[0],float(parts[1]),float(parts[2]),float(parts[3]),float(parts[4])))
                    except Exception: pass
            if len(rows)<30: raise RuntimeError('invalid XAUUSD history')
            closes=[r[4] for r in rows]; highs=[r[2] for r in rows]; lows=[r[3] for r in rows]
            def ema(vals,n):
                k=2/(n+1); e=sum(vals[:n])/n
                for v in vals[n:]: e=v*k+e*(1-k)
                return e
            def rsi(vals,n=14):
                gains=[]; losses=[]
                for i in range(1,len(vals)):
                    d=vals[i]-vals[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
                ag=sum(gains[-n:])/n; al=sum(losses[-n:])/n
                return 100 if al==0 else 100-(100/(1+ag/al))
            close=closes[-1]; e20=ema(closes,20); e50=ema(closes,50); e200=ema(closes,200) if len(closes)>=200 else None
            rs=rsi(closes); tr=[]
            for i in range(1,len(closes)): tr.append(max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])))
            atr=sum(tr[-14:])/14/close*100
            resistance=max(highs[-21:-1]); support=min(lows[-21:-1])
            hh=max(highs[-5:])>max(highs[-10:-5]); hl=min(lows[-5:])>min(lows[-10:-5])
            ch1=(closes[-1]/closes[-2]-1)*100; ch4=(closes[-1]/closes[-5]-1)*100
            s.update(price=close,market_cap=None,liquidity=1000000000.0,volume_24h=None,buys=0,sells=0,change_1h=ch1,change_4h=ch4,holders=None,top10_pct=None,largest_holder_pct=None)
            s['technical']={'close':close,'ema20':e20,'ema50':e50,'ema200':e200,'rsi14':rs,'atr_pct':atr,'resistance':resistance,'support':support,'breakout_pct':(close/resistance-1)*100 if resistance else None,'volume_ratio':1.0,'higher_high':hh,'higher_low':hl,'lower_high':False,'lower_low':False,'pool':'STOOQ_XAUUSD','candles':len(rows)}
            return s
        except Exception as e:
            s['errors'].append('xau:'+str(e)); s['change_4h']=None; s['technical']=None; return s

    def discover(self,maxn=40,cursor=None):
        """Global discovery with a dedicated fresh-pool frontier."""
        feeds=[]; found={}
        def add_item(name,address,source,chain_id=None,meta=None):
            if not str(address or '').strip(): return None
            k=str(address).lower()
            if k in found:
                found[k].setdefault('sources',[])
                if source not in found[k]['sources']: found[k]['sources'].append(source)
                if chain_id and not found[k].get('chain_id'): found[k]['chain_id']=chain_id
                if meta: found[k].update({mk:mv for mk,mv in meta.items() if mv is not None})
                return found[k]
            rec={'name':str(name or 'TOKEN'),'address':str(address),'sources':[source],'chain_id':chain_id,'asset_class':'crypto'}
            if meta: rec.update({mk:mv for mk,mv in meta.items() if mv is not None})
            found[k]=rec; return rec
        fresh=[]
        try:
            data=self.get(f'{GECKO}/networks/new_pools',{'include':'base_token,quote_token,network,dex','page':1},cache_ttl=20)
            included=data.get('included',[]) if isinstance(data,dict) else []
            tokmap={}
            for obj in included:
                if not isinstance(obj,dict) or obj.get('type')!='token': continue
                attrs=obj.get('attributes') or {}; addr=attrs.get('address')
                if addr: tokmap[str(obj.get('id',''))]=attrs
            major={'USDC','USDT','DAI','USDE','USDS','FDUSD','TUSD','USDBC','USD0','WETH','ETH','WBTC','BTC','SOL','WSOL','WBNB','BNB','WMATIC','MATIC','WAVAX','AVAX','WETH.E','SUI','WSUI','CRO','WCRO','WSEI','SEI','WFTM','FTM'}
            netmap={'eth':'ethereum','ethereum':'ethereum','solana':'solana','base':'base','bsc':'bsc','arbitrum':'arbitrum','polygon_pos':'polygon','polygon':'polygon','optimism':'optimism','avax':'avalanche','avalanche':'avalanche','robinhood':'robinhood','linea':'linea','blast':'blast','zksync_era':'zksync','zksync':'zksync','scroll':'scroll','mantle':'mantle','cronos':'cronos','sui-network':'sui','sui':'sui'}
            for pool in (data.get('data',[]) if isinstance(data,dict) else []):
                if not isinstance(pool,dict): continue
                attrs=pool.get('attributes') or {}; rel=pool.get('relationships') or {}
                net_obj=((rel.get('network') or {}).get('data') or {}) if isinstance(rel.get('network'),dict) else {}
                chain_id=netmap.get(str(net_obj.get('id') or ''))
                if chain_id not in GLOBAL_CHAIN_ALLOWLIST: continue
                base_id=str((((rel.get('base_token') or {}).get('data') or {}).get('id')) or ''); quote_id=str((((rel.get('quote_token') or {}).get('data') or {}).get('id')) or '')
                base=tokmap.get(base_id,{}); quote=tokmap.get(quote_id,{})
                bs=str(base.get('symbol') or '').upper().strip(); qs=str(quote.get('symbol') or '').upper().strip()
                chosen=base if bs not in major else (quote if qs not in major else {})
                if not chosen: continue
                address=str(chosen.get('address') or '').strip()
                if not address: continue
                created=attrs.get('pool_created_at'); created_ms=None
                if created:
                    try:
                        from datetime import datetime
                        created_ms=datetime.fromisoformat(str(created).replace('Z','+00:00')).timestamp()*1000
                    except Exception: pass
                vol=attrs.get('volume_usd') or {}
                meta={'pair_created_at':created_ms,'new_pool':True,'pool_created_at':created,'new_pool_liquidity':float(attrs.get('reserve_in_usd') or 0),'new_pool_volume_24h':float(vol.get('h24') or 0) if isinstance(vol,dict) else None}
                before=found.get(address.lower()); rec=add_item(str(chosen.get('name') or chosen.get('symbol') or 'TOKEN'),address,'gecko_new_pool',chain_id,meta)
                if before is None: fresh.append(rec)
        except Exception as e: log.warning('Gecko new-pool discovery failed: %s',e)
        feeds.append(fresh)
        for endpoint in ('/token-profiles/latest/v1','/token-boosts/latest/v1','/token-boosts/top/v1'):
            local=[]
            try:
                x=self.get(DEX+endpoint,cache_ttl=20)
                for item in x if isinstance(x,list) else []:
                    chain_id=str(item.get('chainId') or '')
                    if chain_id not in GLOBAL_CHAIN_ALLOWLIST: continue
                    a=str(item.get('tokenAddress') or '').strip()
                    if not a: continue
                    before=found.get(a.lower()); add_item(item.get('tokenName') or item.get('symbol') or 'TOKEN',a,'dex',chain_id)
                    if before is None: local.append(found[a.lower()])
            except Exception as e: log.warning('discovery %s: %s',endpoint,e)
            feeds.append(local)
        bs_page=[]
        try:
            data=self.get(f'{BLOCKSCOUT}/tokens',{'type':'ERC-20'},cache_ttl=30)
            for item in (data.get('items',[]) if isinstance(data,dict) else []):
                a=item.get('contract_address_hash') or item.get('address') or ''
                if not ADDR_RE.match(str(a)): continue
                before=found.get(str(a).lower()); add_item(item.get('name') or item.get('symbol') or 'TOKEN',a,'blockscout_tokens','robinhood')
                if before is None: bs_page.append(found[str(a).lower()])
        except Exception as e: log.warning('Blockscout token discovery failed: %s',e)
        feeds.append(bs_page)
        activity=[]
        try:
            data=self.get(f'{BLOCKSCOUT}/token-transfers',cache_ttl=15)
            for tr in (data.get('items',[]) if isinstance(data,dict) else []):
                tok=tr.get('token') if isinstance(tr,dict) else None
                if not isinstance(tok,dict): continue
                if str(tok.get('type','')).upper() not in ('ERC-20',''): continue
                a=tok.get('address_hash') or tok.get('contract_address_hash') or ''
                if not ADDR_RE.match(str(a)): continue
                before=found.get(str(a).lower()); add_item(tok.get('name') or tok.get('symbol') or 'TOKEN',a,'blockscout_activity','robinhood')
                if before is None: activity.append(found[str(a).lower()])
        except Exception as e: log.warning('Blockscout activity discovery failed: %s',e)
        feeds.append(activity)
        if maxn<=0: return list(found.values()), None
        out=[]
        while len(out)<maxn and any(feeds):
            progressed=False
            for feed in feeds:
                if feed:
                    out.append(feed.pop(0)); progressed=True
                    if len(out)>=maxn: break
            if not progressed: break
        return out, None
    def discovery_rank(self,item):
        try:
            p=self.dex_pair(item.get('address',''),item.get('chain_id') or CHAIN)
            if not p:
                if item.get('new_pool'):
                    liq=float(item.get('new_pool_liquidity') or 0); vol=float(item.get('new_pool_volume_24h') or 0); created=item.get('pair_created_at'); age_min=9999.0
                    if created:
                        try: age_min=max(0,(time.time()*1000-float(created))/60000)
                        except (TypeError,ValueError): pass
                    freshness=max(0.0,30.0-min(30.0,age_min)*2.0); liq_pts=min(35.0,15.0*min(2.0,liq/25000.0)) if liq>0 else 0.0; vol_pts=min(25.0,10.0*min(2.5,vol/5000.0)) if vol>0 else 0.0
                    return {'rank':round(freshness+liq_pts+vol_pts,2),'market_cap':None,'liquidity':liq,'volume_24h':vol,'change_1h':0.0,'ratio':0.0,'pair_created_at':created}
                return None
            mc=float(p.get('marketCap') or p.get('fdv') or 0); liq=float((p.get('liquidity') or {}).get('usd') or 0); vol=float((p.get('volume') or {}).get('h24') or 0); pc=p.get('priceChange') or {}; ch1=float(pc.get('h1') or 0); tx=(p.get('txns') or {}).get('h24') or {}; buys=float(tx.get('buys') or 0); sells=float(tx.get('sells') or 0); ratio=buys/sells if sells else (99 if buys else 0)
            if liq<=0 and vol<=0: return None
            f=self.cfg.get('filters',{}); mc_min=float(f.get('preferred_market_cap_min',f.get('min_market_cap',1500000))); mc_max=float(f.get('preferred_market_cap_max',f.get('max_market_cap',20000000))); liq_target=float(f.get('preferred_liquidity',f.get('min_liquidity',150000))); vol_target=float(f.get('preferred_volume_24h',f.get('min_volume_24h',250000)))
            if mc>0 and mc_min<=mc<=mc_max: mc_pts=20
            elif mc>0: mc_pts=max(1.0,20-12*min(1.0,abs(mc-max(min(mc,mc_max),mc_min))/max(mc_min,1)))
            else: mc_pts=0
            liq_pts=20*min(1.5,liq/max(liq_target,1))/1.5 if liq>0 else 0; vol_pts=25*min(1.5,vol/max(vol_target,1))/1.5 if vol>0 else 0; flow_pts=15*min(1.5,max(0,ratio))/1.5; momentum=max(0,min(20,10+(min(100,max(-50,ch1))/10)))
            return {'rank':round(mc_pts+liq_pts+vol_pts+flow_pts+momentum,2),'market_cap':mc,'liquidity':liq,'volume_24h':vol,'change_1h':ch1,'ratio':round(ratio,3),'pair_created_at':p.get('pairCreatedAt')}
        except Exception:
            if item.get('new_pool'):
                liq=float(item.get('new_pool_liquidity') or 0); vol=float(item.get('new_pool_volume_24h') or 0)
                return {'rank':round(20+min(25,liq/25000*15)+min(20,vol/5000*10),2),'market_cap':None,'liquidity':liq,'volume_24h':vol,'change_1h':0.0,'ratio':0.0,'pair_created_at':item.get('pair_created_at')}
            return None


def _ema(values,period):
    vals=[float(v) for v in values if v is not None]
    if len(vals)<period: return None
    k=2.0/(period+1.0); e=sum(vals[:period])/period
    for v in vals[period:]: e=v*k+e*(1-k)
    return e

def _rsi(values,period=14):
    vals=[float(v) for v in values if v is not None]
    if len(vals)<period+1: return None
    gains=[]; losses=[]
    for i in range(1,len(vals)):
        d=vals[i]-vals[i-1]; gains.append(max(d,0.0)); losses.append(max(-d,0.0))
    ag=sum(gains[:period])/period; al=sum(losses[:period])/period
    for i in range(period,len(gains)):
        ag=(ag*(period-1)+gains[i])/period; al=(al*(period-1)+losses[i])/period
    if al==0: return 100.0 if ag>0 else 50.0
    rs=ag/al; return 100.0-(100.0/(1.0+rs))

def _aggregate_4h(rows):
    """Aggregate chronological hourly OHLCV rows into 4-hour candles."""
    buckets={}
    for r in rows or []:
        if not isinstance(r,(list,tuple)) or len(r)<6: continue
        try:
            ts=float(r[0]); bucket=int(ts//(4*3600))
            buckets.setdefault(bucket,[]).append(r)
        except (TypeError,ValueError):
            continue
    out=[]
    for bucket,rs in sorted(buckets.items()):
        rs=sorted(rs,key=lambda x:float(x[0]))
        if len(rs)<3: continue
        try:
            out.append((float(rs[0][0]),float(rs[0][1]),max(float(x[2]) for x in rs),min(float(x[3]) for x in rs),float(rs[-1][4]),sum(float(x[5]) for x in rs)))
        except (TypeError,ValueError):
            continue
    return out


def _technical_snapshot(rows,tpool,min_candles=60):
    rows=[r for r in rows if isinstance(r,(list,tuple)) and len(r)>=6]
    rows.sort(key=lambda r:float(r[0]))
    if len(rows)<int(min_candles):
        raise RuntimeError(f'insufficient Gecko technical OHLCV ({len(rows)}; need {int(min_candles)})')
    closes=[float(r[4]) for r in rows]; highs=[float(r[2]) for r in rows]; lows=[float(r[3]) for r in rows]; vols=[float(r[5]) for r in rows]
    close=closes[-1]; ema20=_ema(closes,20); ema50=_ema(closes,50); ema200=_ema(closes,200); rsi14=_rsi(closes,14)
    if close<=0 or ema20 is None or ema50 is None or rsi14 is None:
        raise RuntimeError('technical indicators incomplete')
    trs=[]
    for i in range(1,len(rows)):
        prev=closes[i-1]; trs.append(max(highs[i]-lows[i],abs(highs[i]-prev),abs(lows[i]-prev)))
    atr_pct=(sum(trs[-14:])/14/close*100) if len(trs)>=14 else None
    resistance=max(highs[-21:-1]) if len(highs)>=22 else max(highs[:-1])
    support=min(lows[-21:-1]) if len(lows)>=22 else min(lows[:-1])
    breakout=(close/resistance-1)*100 if resistance>0 else None
    last3=sum(vols[-3:])/3; prev15=sum(vols[-18:-3])/15 if len(vols)>=18 else 0; vol_ratio=last3/prev15 if prev15>0 else None
    hh=max(highs[-5:])>max(highs[-10:-5]) if len(highs)>=10 else False
    hl=min(lows[-5:])>min(lows[-10:-5]) if len(lows)>=10 else False
    lh=max(highs[-5:])<max(highs[-10:-5]) if len(highs)>=10 else False
    ll=min(lows[-5:])<min(lows[-10:-5]) if len(lows)>=10 else False
    bars4=_aggregate_4h(rows)
    c4=[float(r[4]) for r in bars4]
    e20_4=_ema(c4,20); rsi14_4=_rsi(c4,14)
    mtf_trend='BULLISH' if e20_4 is not None and c4[-1]>e20_4 else ('BEARISH' if e20_4 is not None and c4[-1]<e20_4 else 'UNKNOWN')
    return {'close':close,'ema20':ema20,'ema50':ema50,'ema200':ema200,'rsi14':rsi14,'atr_pct':atr_pct,'resistance':resistance,'support':support,'breakout_pct':breakout,'volume_ratio':vol_ratio,'higher_high':hh,'higher_low':hl,'lower_high':lh,'lower_low':ll,'pool':tpool,'candles':len(rows),'mtf_4h_ema20':e20_4,'mtf_4h_rsi14':rsi14_4,'mtf_4h_trend':mtf_trend,'mtf_4h_candles':len(bars4)}


def technical_score(s):
    t=s.get('technical')
    if not isinstance(t,dict): return None,[]
    close=t.get('close'); ema20=t.get('ema20'); ema50=t.get('ema50'); ema200=t.get('ema200'); rsi=t.get('rsi14')
    resistance=t.get('resistance'); vol_ratio=t.get('volume_ratio'); atr=t.get('atr_pct')
    hh=bool(t.get('higher_high')); hl=bool(t.get('higher_low'))
    if any(v is None for v in (close,ema20,ema50,rsi)): return None,['technical_incomplete']
    score=0.0; reasons=[]
    if ema200 is not None and close>ema20>ema50>ema200:
        score+=22; reasons.append('EMA_20_50_200_BULLISH')
    elif close>ema20>ema50:
        score+=17; reasons.append('EMA_20_50_BULLISH')
    elif close>ema20:
        score+=11; reasons.append('EMA_SHORT_BULLISH')
    elif ema50 is not None and close>ema50:
        score+=7; reasons.append('EMA_RECOVERY')
    else:
        reasons.append('EMA_BEARISH')
    if 48<=rsi<=68:
        score+=15; reasons.append('RSI_HEALTHY')
    elif 42<=rsi<48 or 68<rsi<=74:
        score+=10
    elif 35<=rsi<42:
        score+=7
    elif rsi>74:
        score+=4; reasons.append('RSI_OVEREXTENDED')
    else:
        score+=2; reasons.append('RSI_WEAK')
    if resistance and resistance>0:
        bp=(close/resistance-1)*100; t['breakout_pct']=bp
        if bp>=0: score+=18; reasons.append('BREAKOUT')
        elif bp>=-1.5: score+=15; reasons.append('NEAR_BREAKOUT')
        elif bp>=-4: score+=10
        elif bp>=-8: score+=5
    if vol_ratio is not None:
        if vol_ratio>=2: score+=18; reasons.append('VOLUME_CONFIRMATION')
        elif vol_ratio>=1.5: score+=15
        elif vol_ratio>=1.2: score+=12
        elif vol_ratio>=1.0: score+=8
        elif vol_ratio>=0.8: score+=4
    if hh and hl: score+=15; reasons.append('HH_HL')
    elif hh or hl: score+=8; reasons.append('PARTIAL_STRUCTURE')
    if atr is not None:
        if 2<=atr<=10: score+=12
        elif atr<=15: score+=7
        else: score+=2; reasons.append('HIGH_ATR')
    mtf_trend=t.get('mtf_4h_trend')
    mtf_rsi=t.get('mtf_4h_rsi14')
    if mtf_trend=='BULLISH': score+=5; reasons.append('4H_TREND_BULLISH')
    elif mtf_trend=='BEARISH': score-=5; reasons.append('4H_TREND_BEARISH')
    if mtf_rsi is not None and 45<=float(mtf_rsi)<=70:
        score+=3; reasons.append('4H_RSI_HEALTHY')
    return round(max(0,min(100,score)),2),reasons

def classify_setup(t,cfg=None):
    if not isinstance(t,dict) or not t.get('close'): return 'UNKNOWN',None,[]
    c=float(t['close']); r=t.get('resistance'); sup=t.get('support'); e20=t.get('ema20'); e50=t.get('ema50'); e200=t.get('ema200');
    bp=t.get('breakout_pct'); vr=float(t.get('volume_ratio') or 0); hh=bool(t.get('higher_high')); hl=bool(t.get('higher_low')); lh=bool(t.get('lower_high')); ll=bool(t.get('lower_low'))
    reasons=[]; scfg=(cfg or {}).get('score',{}) if isinstance(cfg,dict) else {}
    room=float(scfg.get('pullback_min_resistance_room_pct',1.5))
    # Breakout requires price above resistance and meaningful volume.
    if bp is not None and bp>=0.15 and vr>=1.2:
        zone=[float(r)*0.995,float(r)*1.015] if r else [c*0.99,c*1.01]; reasons+=['breakout','volume_confirmed']; return 'BREAKOUT',zone,reasons
    # Retest/reclaim has precedence when price is close to resistance. Entry is
    # only valid after the resistance is actually reclaimed.
    if r and vr>=1.0 and c>=float(r)*0.98 and c<=float(r)*1.02:
        zone=[float(r)*0.995,float(r)*1.02]; reasons+=['retest','resistance_reclaim']; return 'RETEST',zone,reasons
    # Pullback requires a real higher-low and no active lower-low structure.
    if e20 and c>e20 and sup and c>sup and hl and not ll and not lh:
        if not r or ((float(r)-c)/float(r)*100)>=room:
            zone=[float(e20)*0.985,float(e20)*1.015]; reasons+=['pullback','structure_support']; return 'PULLBACK',zone,reasons
    if e20 and e50 and c>e20 and e20>e50 and (e200 is None or c>=e200) and (hh or hl) and not ll:
        zone=[min(c,float(e20))*0.99,max(c,float(e20))*1.005]; reasons+=['trend_continuation']; return 'TREND_CONTINUATION',zone,reasons
    if e50 and c>e50 and hl and not ll and not lh:
        zone=[float(e50)*0.985,float(e50)*1.015]; reasons+=['reversal','ema_reclaim']; return 'EMA_REVERSAL',zone,reasons
    if bp is not None and bp>3 and vr<1.2:
        reasons+=['chase_risk']; return 'MOMENTUM_CHASE',None,reasons
    return 'NO_SETUP',None,reasons


def score(s,cfg):
    """Risk-aware opportunity score for crypto and non-crypto market instruments.
    Market-cap/holder preferences are never universal hard gates.
    """
    if s.get('asset_class')=='commodity':
        t=s.get('technical') if isinstance(s.get('technical'),dict) else {}
        tv,tr=technical_score(s)
        setup,zone,sr=classify_setup(t,cfg)
        reasons=list(tr)+list(sr); risk=[]
        ch1=s.get('change_1h'); ch4=s.get('change_4h')
        if ch1 is not None and ch1>float(cfg['filters'].get('max_1h_change',20)): risk.append('HOT_MOMENTUM'); reasons.append('1h_hot')
        if ch4 is not None and ch4<float(cfg['filters'].get('min_4h_change',-15)): risk.append('4h_weak')
        scorev=float(tv or 0)
        if setup=='MOMENTUM_CHASE': risk.append('CHASE_RISK'); scorev-=float(cfg.get('score',{}).get('chase_penalty_max',10))
        scorev=max(0,min(100,scorev))
        verdict='BUY CANDIDATE' if setup not in ('UNKNOWN','NO_SETUP') and scorev>=float(cfg['score']['min_buy_candidate']) else ('WATCH' if scorev>=float(cfg['score']['min_watch']) else 'WAIT')
        return {'name':s['name'],'address':s['address'],'score':round(scorev,2),'base_score':round(scorev,2),'technical_score':round(scorev,2) if tv is not None else None,'raw_score':round(scorev,2),'verdict':verdict,'ratio':0,'liq_mc':0,'reasons':reasons,'risk_flags':sorted(set(risk)),'missing':[] if t else ['technical'],'setup_type':setup,'entry_zone':zone}

    """Risk-aware score: core market/liquidity gates stay hard; concentration and
    extreme momentum are graded/penalized instead of causing automatic rejection.
    All thresholds remain configurable in filters/score settings.
    """
    f=cfg['filters']; w=cfg['score']['weights']; pts=0.0; hard=False; miss=[]; reasons=[]; risk_flags=[]
    def num(k): return s.get(k)
    mc,liq,vol,h,top,largest,ch1,ch4=[num(x) for x in ('market_cap','liquidity','volume_24h','holders','top10_pct','largest_holder_pct','change_1h','change_4h')]

    # Professional-trader style: market cap and holder count are preferences,
    # not walls. Small opportunities stay eligible and receive proportional score/risk.
    if mc is None: miss.append('market_cap')
    elif f['min_market_cap']<=mc<=f['max_market_cap']: pts+=w['market_cap']
    elif mc>0:
        pts+=w['market_cap']*min(0.95,max(0.08,mc/max(float(f['min_market_cap']),1)))
        reasons.append('SMALL_OR_OUTSIDE_PREFERRED_MC')
        risk_flags.append('SMALL_CAP' if mc<float(f['min_market_cap']) else 'LARGE_CAP')
    if liq is None: miss.append('liquidity')
    elif liq>=f['min_liquidity']: pts+=w['liquidity']
    elif liq>=float(f.get('hard_min_liquidity',25000)):
        pts+=w['liquidity']*min(0.95,max(0.10,liq/max(float(f['min_liquidity']),1)))
        reasons.append('LOW_LIQUIDITY'); risk_flags.append('LOW_LIQUIDITY')
    else:
        hard=True; reasons.append('LIQUIDITY_SAFETY_FLOOR'); risk_flags.append('LOW_LIQUIDITY')

    # Volume and holders are scored progressively rather than binary.
    if vol is None: miss.append('volume_24h')
    elif vol>=f['min_volume_24h']: pts+=w['volume']
    else: pts+=w['volume']*min(1.0, max(0.0, vol/f['min_volume_24h']))
    if h is None: miss.append('holders')
    elif h>=f['min_holders']: pts+=w['holders']
    else: pts+=w['holders']*min(1.0, max(0.0, h/f['min_holders']))

    # Holder concentration: target limits earn full points; excess is a penalty,
    # with only extreme concentration becoming a hard rejection.
    top_hard=float(f.get('top10_hard_limit', max(60.0, float(f['max_top10_pct'])*2)))
    if top is None: miss.append('top10_pct')
    elif top<=f['max_top10_pct']:
        pts+=w['top10']
    elif top<top_hard:
        frac=1.0-(top-f['max_top10_pct'])/(top_hard-f['max_top10_pct'])
        pts+=w['top10']*max(0.0,frac)
        reasons.append('top10_above_target'); risk_flags.append('CONCENTRATION')
    else:
        hard=True; reasons.append('top10_extreme'); risk_flags.append('EXTREME_CONCENTRATION')

    largest_hard=float(f.get('largest_holder_hard_limit', max(35.0, float(f['max_largest_holder_pct'])*2)))
    if largest is None: miss.append('largest_holder_pct')
    elif largest<=f['max_largest_holder_pct']:
        pts+=w['largest']
    elif largest<largest_hard:
        frac=1.0-(largest-f['max_largest_holder_pct'])/(largest_hard-f['max_largest_holder_pct'])
        pts+=w['largest']*max(0.0,frac)
        reasons.append('largest_holder_above_target'); risk_flags.append('CONCENTRATION')
    else:
        hard=True; reasons.append('largest_holder_extreme'); risk_flags.append('EXTREME_CONCENTRATION')

    buys=s.get('buys') or 0; sells=s.get('sells') or 0; ratio=buys/sells if sells else (99 if buys else 0)
    if ratio>=f['min_buy_sell_ratio']:
        pts+=w['flow']
    else:
        pts+=w['flow']*min(1.0,max(0.0,ratio/f['min_buy_sell_ratio']))
        reasons.append('sell_pressure')

    # 1H momentum is no longer a hard reject. Very fast moves lose some score
    # and receive a HOT_MOMENTUM flag, while still remaining discoverable.
    if ch1 is None: miss.append('change_1h')
    else:
        hot1=float(f.get('hot_1h_level_1',80)); hot2=float(f.get('hot_1h_level_2',150)); hot3=float(f.get('hot_1h_level_3',300)); target=float(f['max_1h_change'])
        if ch1<=target: pts+=w['momentum_1h']
        elif ch1<=hot1: pts+=w['momentum_1h']*0.80; reasons.append('1h_hot'); risk_flags.append('HOT_MOMENTUM')
        elif ch1<=hot2: pts+=w['momentum_1h']*0.60; reasons.append('1h_very_hot'); risk_flags.append('HOT_MOMENTUM')
        elif ch1<=hot3: pts+=w['momentum_1h']*0.40; reasons.append('1h_extreme'); risk_flags.append('HOT_MOMENTUM')
        else: pts+=w['momentum_1h']*0.20; reasons.append('1h_extreme'); risk_flags.append('HOT_MOMENTUM')

    if ch4 is None: miss.append('change_4h')
    elif ch4>=f['min_4h_change']: pts+=w['momentum_4h']
    else:
        reasons.append('4h_weak')
        if ch4>f['min_4h_change']-30: pts+=w['momentum_4h']*0.5

    liqmc=liq/mc*100 if liq and mc else 0
    if liqmc>=f['min_liquidity_mc_pct']: pts+=10
    elif liqmc>0: pts+=10*min(1.0, max(0.0,liqmc/f['min_liquidity_mc_pct'])); reasons.append('liq_mc')
    else: reasons.append('liq_mc')

    max_points=sum(float(v) for v in w.values())+10.0
    base_normalized=pts/max_points*100 if max_points else 0.0
    normalized=base_normalized

    tech_value,tech_reasons=technical_score(s)
    if tech_value is not None:
        normalized=base_normalized*0.60+tech_value*0.40
        reasons.extend(tech_reasons)

    # Small transparent penalties keep the score calibrated without deleting
    # otherwise strong setups. These values are configurable.
    if top is not None and top>f['max_top10_pct'] and top<top_hard:
        normalized-=float(cfg['score'].get('top10_excess_penalty_max',8.0))*min(1.0,(top-f['max_top10_pct'])/(top_hard-f['max_top10_pct']))
    if largest is not None and largest>f['max_largest_holder_pct'] and largest<largest_hard:
        normalized-=float(cfg['score'].get('largest_excess_penalty_max',10.0))*min(1.0,(largest-f['max_largest_holder_pct'])/(largest_hard-f['max_largest_holder_pct']))
    if ch1 is not None and ch1>f['max_1h_change']:
        normalized-=float(cfg['score'].get('hot_1h_penalty_max',12.0))*min(1.0,(ch1-f['max_1h_change'])/max(1.0,hot3-f['max_1h_change']))
    normalized=max(0.0,min(100.0,normalized))

    setup,entry_zone,setup_reasons=classify_setup(s.get('technical'),cfg)
    if setup=='MOMENTUM_CHASE': risk_flags.append('CHASE_RISK'); normalized-=float(cfg.get('score',{}).get('chase_penalty_max',10))
    reasons.extend(setup_reasons)
    normalized=max(0.0,min(100.0,normalized))
    if hard:
        verdict='REJECT'
    elif miss:
        verdict='DATA_INCOMPLETE'
    elif setup in ('UNKNOWN','NO_SETUP'):
        # A high score without a tradable setup is a watch idea, not a buy setup.
        verdict='WATCH' if normalized>=cfg['score']['min_watch'] else 'WAIT'
    else:
        verdict='BUY CANDIDATE' if normalized>=cfg['score']['min_buy_candidate'] else ('WATCH' if normalized>=cfg['score']['min_watch'] else 'WAIT')
    return {'name':s['name'],'address':s['address'],'score':round(normalized,2),'base_score':round(base_normalized,2),'technical_score':tech_value,'raw_score':round(pts,2),'verdict':verdict,'ratio':round(ratio,3),'liq_mc':round(liqmc,2),'reasons':reasons,'risk_flags':sorted(set(risk_flags)),'missing':miss,'setup_type':setup,'entry_zone':entry_zone}


def _portfolio_equity(st):
    d=st.get('demo',{})
    cash=float(d.get('cash',0) or 0)
    invested=sum(float(p.get('invested',0) or 0) for p in (d.get('positions',{}) or {}).values())
    return max(0.0,cash+invested)

def _demo_amount(st,cfg,info):
    """Professional position sizing: allocation target + hard risk cap.
    The trade amount is a capital allocation, while risk_per_trade_pct limits
    the loss implied by the stop. With the defaults, $20 equity targets 25%
    ($5) per position and risks at most 2% ($0.40) when the stop is 8%.
    """
    r=cfg['risk']; price=float(info.get('price') or 0)
    if price<=0:return 0.0
    cash=float(st['demo'].get('cash',0) or 0); equity=_portfolio_equity(st)
    max_trade=float(r['demo_max_per_trade_usd'])
    if not r.get('dynamic_sizing',True):
        target=equity*float(r.get('allocation_per_trade_pct',25.0))/100.0
        return max(0.0,min(max_trade,cash,target))
    stop_pct=float(info.get('suggested_stop_pct') or r.get('stop_loss_pct',8))
    stop_pct=max(0.25,stop_pct)
    risk_usd=equity*float(r.get('risk_per_trade_pct',2.0))/100.0
    target_alloc=equity*float(r.get('allocation_per_trade_pct',25.0))/100.0
    risk_limited=risk_usd/(stop_pct/100.0)
    return max(0.0,min(max_trade,cash,target_alloc,risk_limited))

def risk_buy(st,cfg,info,amount=None):
    r=cfg['risk']; rollover(st)
    if st['panic']: return None,'PANIC STOP'
    cooldown_min=float(cfg.get('signal',{}).get('entry_cooldown_minutes',0))
    a=str(info.get('address','')).lower(); last_entry=float(st.setdefault('entry_cooldowns',{}).get(a,0) or 0)
    if cooldown_min>0 and time.time()-last_entry < cooldown_min*60: return None,'entry cooldown'
    if st['today']['loss']>=r['max_daily_loss_usd']: return None,'daily loss limit'
    if st['today']['trades']>=r['max_trades_per_day']: return None,'daily trade limit'
    if len(st['demo']['positions'])>=r['max_open_positions']: return None,'max open positions'
    equity=_portfolio_equity(st); invested=sum(float(p.get('invested',0) or 0) for p in st['demo']['positions'].values())
    portfolio_cap=equity*float(r.get('max_portfolio_allocation_pct',75.0))/100.0
    remaining_cap=max(0.0,portfolio_cap-invested)
    if remaining_cap<=0:return None,'portfolio allocation limit'
    if amount is None: amount=_demo_amount(st,cfg,info)
    amount=min(float(amount),float(r['demo_max_per_trade_usd']),float(st['demo']['cash']),remaining_cap)
    if amount<=0:return None,'trade/cash/portfolio limit'
    price=float(info.get('price') or 0)
    if price<=0:return None,'invalid price'
    stop_pct=float(info.get('suggested_stop_pct') or r.get('stop_loss_pct',8)); stop_pct=max(0.25,stop_pct)
    risk_usd=amount*(stop_pct/100.0)
    max_risk_usd=equity*float(r.get('risk_per_trade_pct',2.0))/100.0
    if risk_usd>max_risk_usd+1e-9:
        amount=min(amount,max_risk_usd/(stop_pct/100.0))
    if amount<=0:return None,'risk limit'
    tid='D-'+uuid.uuid4().hex[:10]
    stop_price=price*(1-stop_pct/100)
    st.setdefault('entry_cooldowns',{})[a]=time.time(); st['demo']['cash']-=amount
    st['demo']['positions'][a]={'trade_id':tid,'name':info['name'],'address':info['address'],'qty':amount/price,'entry':price,'invested':amount,'opened_at':time.time(),'high':price,'score':info.get('score'),'setup_type':info.get('setup_type','UNKNOWN'),'entry_zone':info.get('entry_zone'),'stop_price':stop_price,'partial_taken':False,'be_price':price*(1+float(r.get('break_even_trigger_pct',5))/100),'risk_at_stop_usd':risk_usd,'allocation_pct':amount/equity*100 if equity else 0}
    st['today']['trades']+=1
    return tid,'OK'

def risk_sell(st,cfg,a,price,reason,qty=None):
    p=st['demo']['positions'].get(a.lower())
    if not p:return None,'not found'
    qty=float(p['qty'] if qty is None else min(qty,p['qty']))
    if qty<=0:return None,'invalid qty'
    invested_piece=float(p['invested'])*(qty/float(p['qty']))
    proceeds=qty*price; pnl=proceeds-invested_piece; st['demo']['cash']+=proceeds; st['demo']['realized_pnl']+=pnl
    if qty>=float(p['qty'])*0.999999:
        del st['demo']['positions'][a.lower()]
    else:
        p['qty']-=qty; p['invested']-=invested_piece
    if pnl<0: st['today']['loss']+=abs(pnl)
    tr={**p,'qty_closed':qty,'exit':price,'proceeds':proceeds,'pnl':pnl,'pnl_pct':pnl/invested_piece*100 if invested_piece else 0,'reason':reason,'closed_at':time.time()}
    st['demo']['trades'].append(tr); return tr,'OK'

def manage_position(st,cfg,info):
    a=info['address'].lower(); p=st['demo']['positions'].get(a)
    if not p:return None
    price=float(info.get('price') or 0); r=cfg['risk']; p['high']=max(p.get('high',p['entry']),price)
    t=info.get('technical') or {}; atr=float(t.get('atr_pct') or 0)
    if atr>0: p['stop_price']=max(float(p.get('stop_price') or 0),price*(1-float(r.get('atr_stop_multiplier',1.5))*atr/100)) if price>p['entry'] else float(p.get('stop_price') or p['entry']*(1-float(r.get('stop_loss_pct',8))/100))
    if price<=float(p.get('stop_price') or p['entry']*(1-float(r.get('stop_loss_pct',8))/100)): return risk_sell(st,cfg,a,price,'smart-stop')[0]
    if not p.get('partial_taken') and price>=p['entry']*(1+float(r.get('partial_tp_pct',10))/100):
        qty=float(p['qty'])*float(r.get('partial_tp_fraction',0.5)); tr,_=risk_sell(st,cfg,a,price,'partial-take-profit',qty);
        if a in st['demo']['positions']: st['demo']['positions'][a]['partial_taken']=True; st['demo']['positions'][a]['stop_price']=max(float(st['demo']['positions'][a].get('stop_price',0)),p['entry'])
        return tr
    if price>=p['entry']*(1+float(r.get('take_profit_pct',15))/100): return risk_sell(st,cfg,a,price,'take-profit')[0]
    trail=float(r.get('trailing_stop_pct',6)); be=float(r.get('break_even_trigger_pct',5))
    if p['high']>=p['entry']*(1+be/100): p['stop_price']=max(float(p.get('stop_price',0)),p['entry'])
    if p['high']>p['entry'] and price<=p['high']*(1-trail/100): return risk_sell(st,cfg,a,price,'trailing-stop')[0]
    setup=info.get('setup_type'); t=info.get('technical') or {}
    if t.get('ema20') and t.get('ema50') and price<float(t['ema20']) and price<float(t['ema50']) and bool(t.get('lower_high')) and bool(t.get('lower_low')):
        return risk_sell(st,cfg,a,price,'trend-failure')[0]
    if setup=='MOMENTUM_CHASE' or ('CHASE_RISK' in (info.get('risk_flags') or [])): return risk_sell(st,cfg,a,price,'trend-failure/chase')[0]
    return None


def report(st,days=None):
    xs=st['demo']['trades']; cutoff=time.time()-days*86400 if days else None; xs=[x for x in xs if not cutoff or x.get('closed_at',0)>=cutoff]; ps=[float(x.get('pnl',0)) for x in xs]; wins=[p for p in ps if p>0]; losses=[p for p in ps if p<0]
    gross_win=sum(wins); gross_loss=abs(sum(losses)); eq=0.0; peak=0.0; max_dd=0.0
    for p in ps:
        eq+=p; peak=max(peak,eq); max_dd=max(max_dd,peak-eq)
    setups={}
    for x in xs:
        k=x.get('setup_type','UNKNOWN'); q=setups.setdefault(k,{'trades':0,'pnl':0.0,'wins':0}); q['trades']+=1; q['pnl']+=float(x.get('pnl',0)); q['wins']+=1 if float(x.get('pnl',0))>0 else 0
    for q in setups.values(): q['pnl']=round(q['pnl'],4); q['win_rate']=round(q['wins']/q['trades']*100,2) if q['trades'] else 0
    return {'trades':len(xs),'pnl':round(sum(ps),4),'win_rate':round(len(wins)/len(xs)*100,2) if xs else 0,'best':round(max(ps),4) if ps else 0,'worst':round(min(ps),4) if ps else 0,'profit_factor':round(gross_win/gross_loss,3) if gross_loss else (999.0 if gross_win else 0.0),'max_drawdown':round(max_dd,4),'by_setup':setups}


def early_listing_report(st, limit=10):
    xs=st.get('early_listing_watch',[]) if isinstance(st.get('early_listing_watch',[]),list) else []
    xs=xs[-max(1,int(limit)):][::-1]
    if not xs: return '🚀 Early Listings\n\nموردی ثبت نشده است.'
    lines=['🚀 EARLY LISTINGS','']
    for x in xs:
        lines.append(f"• {x.get('name','TOKEN')} | age {x.get('pair_age_minutes','?')}m | 5m {float(x.get('change_5m',0) or 0):+.2f}% | B/S {x.get('buys_5m',0)}/{x.get('sells_5m',0)} | liq ${float(x.get('liquidity',0) or 0):,.0f}")
    return '\n'.join(lines)[:3900]

def totp_valid(secret, code, step=30, window=1):
    try:
        key=base64.b32decode(secret.strip().replace(' ','').upper() + '='*((8-len(secret.strip().replace(' ','').upper())%8)%8),casefold=True)
        code=str(code).strip()
        if not code.isdigit() or len(code)!=6: return False
        counter=int(time.time()//step)
        for offset in range(-window,window+1):
            msg=struct.pack('>Q',counter+offset)
            digest=hmac.new(key,msg,hashlib.sha1).digest()
            pos=digest[-1]&15
            val=(struct.unpack('>I',digest[pos:pos+4])[0]&0x7fffffff)%1000000
            if hmac.compare_digest(f'{val:06d}',code): return True
        return False
    except Exception:
        return False

class Bot:
    def __init__(self,cfg_path='config.json',state_path='state.json'):
        self.cfg_path=cfg_path; self.cfg=load_json(cfg_path,{}); self.state_path=state_path; self.st=load_json(state_path,default_state(self.cfg))
        # V5.5.1 could persist every discovered token into watchlist. If an older
        # state file contains a large discovery backlog, scanning it serially can
        # exceed the GitHub Actions time limit. Keep configured/manual watch items
        # separate from per-scan discovery candidates.
        self._migrate_watchlist_state()
        rollover(self.st); self.api=API(self.cfg); self.lock=threading.Lock()
    def _migrate_watchlist_state(self):
        configured=self.cfg.get('watchlist',[]) if isinstance(self.cfg.get('watchlist',[]),list) else []
        old=self.st.get('watchlist',[]) if isinstance(self.st.get('watchlist',[]),list) else []
        manual=self.st.get('manual_watchlist')
        cap=max(0,int(self.cfg.get('discovery',{}).get('manual_watchlist_max',20)))
        configured_addrs={str(x.get('address','')).lower() for x in configured if isinstance(x,dict) and ADDR_RE.match(str(x.get('address','')))}
        if not isinstance(manual,list): manual=[] if len(old)>max(cap,20) else [x for x in old if isinstance(x,dict)]
        clean=[]; seen=set()
        for x in manual:
            a=str(x.get('address','')).strip() if isinstance(x,dict) else ''
            if not ADDR_RE.match(a) or a.lower() in configured_addrs or a.lower() in seen: continue
            clean.append({'name':str(x.get('name','TOKEN')).upper(),'address':a}); seen.add(a.lower())
        self.st['manual_watchlist']=clean[:cap]; self.st['watchlist']=configured
        self.st['version']='5.10.2'; self.st['state_schema']='5.10.2'
        for k,v in {'entry_cooldowns':{},'last_alert':{},'signal_active':{},'signal_last_score':{},'signal_setup':{},'opportunity_queue':{},'deep_cursor':0,'priority_cursor':0,'blockscout_token_cursor':None,'scan_cycle':0,'opportunity_alerts':{},'last_opportunity_digest':0,'early_listing_watch':[]}.items(): self.st.setdefault(k,v)
    def save(self): atomic_json(self.state_path,self.st)

    def early_listing_signal(self, item):
        cfg=self.cfg.get('early_listing', {})
        if not cfg.get('enabled', True): return None
        a=item.get('address','')
        try: p=self.api.dex_pair(a,item.get('chain_id') or CHAIN)
        except Exception: return None
        if not p: return None
        created=p.get('pairCreatedAt')
        if not created: return None
        age_min=max(0, (time.time()*1000-float(created))/60000)
        if age_min > float(cfg.get('candidate_max_age_minutes',5)): return None
        liq=float((p.get('liquidity') or {}).get('usd') or 0)
        vol5=float((p.get('volume') or {}).get('m5') or 0)
        tx5=(p.get('txns') or {}).get('m5') or {}
        buys5=int(tx5.get('buys') or 0); sells5=int(tx5.get('sells') or 0)
        ratio=buys5/sells5 if sells5 else (99 if buys5 else 0)
        ch5=float((p.get('priceChange') or {}).get('m5') or 0)
        mc=float(p.get('marketCap') or p.get('fdv') or 0)
        if liq < float(cfg.get('min_liquidity_usd',50000)): return None
        if vol5 < float(cfg.get('min_volume_5m_usd',10000)): return None
        if buys5 < int(cfg.get('min_buys_5m',8)): return None
        if ratio < float(cfg.get('min_buy_sell_ratio_5m',1.10)): return None
        if ch5 > float(cfg.get('max_price_change_5m',80)): return None
        if mc and mc < float(cfg.get('min_market_cap_usd',100000)): return None
        if mc and mc > float(cfg.get('max_market_cap_usd',20000000)): return None
        return {'name':item.get('name') or (p.get('baseToken') or {}).get('symbol') or 'TOKEN','address':a,'price':float(p.get('priceUsd') or 0),'market_cap':mc,'liquidity':liq,'volume_5m':vol5,'buys_5m':buys5,'sells_5m':sells5,'buy_sell_ratio_5m':round(ratio,2),'change_5m':ch5,'pair_age_minutes':round(age_min,2),'pair_address':p.get('pairAddress'),'early_listing':True}

    def telegram_targets(self):
        ids={str(x) for x in self.cfg.get('telegram',{}).get('admin_chat_ids',[]) if str(x).strip()}
        env_chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
        if env_chat: ids.add(env_chat)
        return sorted(ids)

    def send_telegram(self,text):
        if not self.cfg.get('telegram',{}).get('enabled',True): return False
        token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
        targets=self.telegram_targets()
        if not token or not targets: return False
        ok=False
        for chat in targets:
            try:
                r=self.api.s.post(TG.format(token,'sendMessage'),json={'chat_id':chat,'text':text},timeout=10)
                r.raise_for_status(); ok=True
            except Exception as e:
                log.warning('Telegram alert failed: %s',e)
        return ok

    def format_signal_alert(self,sig):
        flags=', '.join(sig.get('risk_flags') or []) or 'none'
        return (f"🚨 CONFIRMED | {sig.get('name')}\n"
                f"Score: {sig.get('score')} | confirmations: {sig.get('confirmations')}/{sig.get('required_confirmations')}\n"
                f"Asset: {sig.get('asset_class','crypto')} | Chain: {sig.get('chain_id','')}\nPrice: ${float(sig.get('price') or 0):.8g} | MC: {'n/a' if sig.get('market_cap') is None else f'${float(sig.get("market_cap") or 0):,.0f}'}\n"
                f"Liquidity: ${float(sig.get('liquidity') or 0):,.0f} | 1H: {sig.get('change_1h')}% | 4H: {sig.get('change_4h')}%\n"
                f"Setup: {sig.get('setup_type','UNKNOWN')} | Entry zone: {sig.get('entry_zone')}\n"
                f"Risk: {flags}\n"
                f"Demo: {sig.get('demo_entry','not entered')}")

    def allowed_chat(self,chat):
        return str(chat) in set(self.telegram_targets())
    def _opportunity_messages(self, results, now):
        """Surface good opportunities even when entry is not ready.
        A professional scanner should expose WATCH/BUY-CANDIDATE ideas instead of
        only Telegram-confirmed entries. Alerts are deduplicated by score/setup
        changes so one token cannot dominate the channel.
        """
        cfg=self.cfg.get('signal',{})
        if not cfg.get('telegram_opportunity_alerts', False): return []
        cooldown=float(cfg.get('opportunity_alert_cooldown_seconds',1800))
        candidates=[x for x in results if x.get('verdict') in ('WATCH','BUY CANDIDATE') and not x.get('signal')]
        candidates.sort(key=lambda x:(float(x.get('score') or 0), float(x.get('technical_score') or 0)), reverse=True)
        messages=[]
        alerts=self.st.setdefault('opportunity_alerts',{})
        for x in candidates[:3]:
            key=str(x.get('address','')).lower()
            if not key: continue
            rec=alerts.get(key,{})
            last=float(rec.get('ts',0) or 0)
            setup=x.get('setup_type','UNKNOWN'); entry=bool(x.get('entry_ready'))
            if last and now-last<cooldown: continue
            alerts[key]={'ts':now,'score':float(x.get('score') or 0),'setup':setup,'entry_ready':entry,'name':x.get('name')}
            flags=', '.join(x.get('risk_flags') or []) or 'none'
            messages.append((f"👀 OPPORTUNITY | {x.get('name')}\n"
                             f"Score: {x.get('score')} | {x.get('verdict')} | Tech: {x.get('technical_score')}\n"
                             f"Price: ${float(x.get('price') or 0):.8g} | MC: ${float(x.get('market_cap') or 0):,.0f}\n"
                             f"Asset: {x.get('asset_class','crypto')} | Chain: {x.get('chain_id','')}\nLiq: ${float(x.get('liquidity') or 0):,.0f} | 1H: {x.get('change_1h')}% | 4H: {x.get('change_4h')}%\n"
                             f"Setup: {setup} | Entry ready: {entry}\n"
                             f"Risk: {flags}"))
        return messages

    def scan(self):
        with self.lock:
            rollover(self.st); now=time.time(); self.st['scan_cycle']=int(self.st.get('scan_cycle',0))+1
            configured=self.cfg.get('watchlist',[]) if isinstance(self.cfg.get('watchlist',[]),list) else []
            manual=self.st.get('manual_watchlist',[]) if isinstance(self.st.get('manual_watchlist',[]),list) else []
            # Configured/manual watch items are monitoring hints only. They are
            # seeded into the same opportunity queue as discovered assets, with no
            # priority score or reserved scan slot. This prevents a fixed token from
            # monopolizing deep scans simply because its name is present in config.
            watch_seed={}
            for x in configured+manual:
                if not isinstance(x,dict): continue
                a=str(x.get('address','')).strip()
                if not a or a.lower() in watch_seed: continue
                watch_seed[a.lower()]={'name':str(x.get('name','TOKEN')).upper(),'address':a,
                                      'chain_id':x.get('chain_id') or CHAIN,
                                      'asset_class':x.get('asset_class','crypto')}
            discovery_ranked=[]
            if self.cfg.get('discovery',{}).get('enabled'):
                pool_size=max(1,int(self.cfg.get('discovery',{}).get('candidate_pool_per_scan',40)))
                discovered,next_bs_cursor=self.api.discover(pool_size,None)
                self.st['blockscout_token_cursor']=None
                if self.cfg.get('macro_markets',{}).get('gold',{}).get('enabled',True):
                    discovered.append({'name':'XAUUSD GOLD','address':'XAUUSD','chain_id':'macro','asset_class':'commodity','sources':['macro_gold']})
                q=self.st.setdefault('opportunity_queue',{})
                for x in discovered:
                    a=str(x.get('address','')).strip();
                    if not a: continue
                    if x.get('asset_class')=='commodity':
                        r={'rank':88.0,'market_cap':None,'liquidity':1000000000.0,'volume_24h':None,'change_1h':None,'ratio':0,'pair_created_at':None}
                    else:
                        r=self.api.discovery_rank(x)
                        if not r: continue
                    k=a.lower(); rec=q.get(k,{})
                    rec.update({'name':str(x.get('name','TOKEN')).upper(),'address':a,'chain_id':x.get('chain_id') or CHAIN,'asset_class':x.get('asset_class','crypto'),'sources':x.get('sources',[]),'fast_rank':r['rank'],'market_cap':r.get('market_cap'),'liquidity':r.get('liquidity'),'volume_24h':r.get('volume_24h'),'change_1h':r.get('change_1h'),'ratio':r.get('ratio'),'pair_created_at':r.get('pair_created_at'),'last_seen':now})
                    rec.setdefault('last_deep_scan',0); rec.setdefault('next_deep_at',0); q[k]=rec
                    discovery_ranked.append((r['rank'],x,r))
                queue_cap=max(100,int(self.cfg.get('discovery',{}).get('queue_max_size',500)))
                if len(q)>queue_cap:
                    keep={str(x.get('address','')).lower() for x in configured+manual if isinstance(x,dict)}
                    ordered=sorted(q.items(),key=lambda kv:(kv[1].get('address','').lower() in keep,float(kv[1].get('last_seen',0))),reverse=True)
                    self.st['opportunity_queue']=dict(ordered[:queue_cap]); q=self.st['opportunity_queue']
                discovery_ranked.sort(key=lambda z:z[0],reverse=True)
                watch_cap=max(1,int(self.cfg.get('discovery',{}).get('watchlist_size',50)))
                # Rebuild the active daily watch from the best currently-known opportunities,
                # while preserving tokens already in the queue so they can rotate into deep scans.
                candidates=sorted(q.values(),key=lambda z:(float(z.get('fast_rank',0)),float(z.get('last_seen',0))),reverse=True)
                active=candidates[:watch_cap]
                self.st['daily_watch']=[{'name':x['name'],'address':x['address'],'rank':x.get('fast_rank',0),'last_deep_scan':x.get('last_deep_scan',0),'next_deep_at':x.get('next_deep_at',0),'setup_type':x.get('setup_type','UNKNOWN'),'score':x.get('score')} for x in active]
                src_counts={};
                for x in discovered:
                    for src in (x.get('sources') or ['unknown']): src_counts[src]=src_counts.get(src,0)+1
                log.info('DISCOVERY | requested=%d | unique=%d | sources=%s | queue=%d | active_watch=%d',pool_size,len(discovered),src_counts,len(q),len(active))
            # V5.10.1: one bounded Deep Scan budget is shared by all queued
            # opportunities. Configured/manual watch items are seeded into that
            # same queue but receive NO reserved slot and NO priority bonus.
            # Open demo positions remain the sole exception because they require
            # mandatory position-management checks.
            deep_n=max(1,int(self.cfg.get('discovery',{}).get('deep_scan_candidates_per_scan',6)))
            early_cfg=self.cfg.get('early_listing',{})
            q=self.st.setdefault('opportunity_queue',{})
            for key,item in watch_seed.items():
                rec=q.get(key,{})
                if not rec:
                    rec={'name':item['name'],'address':item['address'],'chain_id':item['chain_id'],
                         'asset_class':item['asset_class'],'sources':['configured_watch'],
                         'fast_rank':0.0,'market_cap':None,'liquidity':None,'volume_24h':None,
                         'change_1h':None,'ratio':0,'pair_created_at':None,'last_seen':now,
                         'last_deep_scan':0,'next_deep_at':0}
                    # If the token is tradeable, calculate its normal discovery rank.
                    # Failure leaves it neutral rather than promoting it.
                    if item['asset_class']=='crypto':
                        try:
                            rr=self.api.discovery_rank(item)
                            if rr:
                                rec.update({'fast_rank':rr.get('rank',0),'market_cap':rr.get('market_cap'),
                                            'liquidity':rr.get('liquidity'),'volume_24h':rr.get('volume_24h'),
                                            'change_1h':rr.get('change_1h'),'ratio':rr.get('ratio'),
                                            'pair_created_at':rr.get('pair_created_at')})
                        except Exception:
                            pass
                    q[key]=rec
                else:
                    # Keep monitoring metadata fresh without changing its priority.
                    rec['name']=item['name']; rec['chain_id']=item['chain_id']; rec['asset_class']=item['asset_class']
                    rec.setdefault('sources',[])
                    if 'configured_watch' not in rec['sources']: rec['sources'].append('configured_watch')

            due=[]
            for rec in q.values():
                if float(rec.get('next_deep_at',0) or 0)<=now: due.append(rec)
            due.sort(key=lambda z:(float(z.get('last_deep_scan',0) or 0),-float(z.get('fast_rank',0) or 0)))

            open_priority=[]
            for a,p in self.st.get('demo',{}).get('positions',{}).items():
                if not str(a).strip(): continue
                open_priority.append({'name':str(p.get('name','TOKEN')).upper(),'address':str(a),'_priority':2000})

            budget_left=max(0,deep_n-len(open_priority))
            remaining=budget_left
            # Reserve part of the discovery budget for genuinely fresh pairs.
            # This prevents established high-ranked tokens from consuming the
            # entire deep-scan budget and gives new listings a real chance to be
            # examined early.
            early_reserve=max(0,int(self.cfg.get('discovery',{}).get('early_listing_reserve',0) or 0))
            early_due=[]; normal_due=[]
            for rec in due:
                created=rec.get('pair_created_at')
                is_early=False
                if created:
                    try:
                        age_min=max(0,(time.time()*1000-float(created))/60000)
                        is_early=age_min<=float(early_cfg.get('candidate_max_age_minutes',5))
                    except (TypeError,ValueError):
                        is_early=False
                (early_due if is_early else normal_due).append(rec)
            early_due.sort(key=lambda z:(float(z.get('last_deep_scan',0) or 0),-float(z.get('fast_rank',0) or 0)))
            normal_due.sort(key=lambda z:(float(z.get('last_deep_scan',0) or 0),-float(z.get('fast_rank',0) or 0)))
            early_take=min(early_reserve,remaining,len(early_due))
            chosen_early=early_due[:early_take]
            chosen_ids={x['address'].lower() for x in chosen_early}
            rest=[x for x in (normal_due+early_due[early_take:]) if x['address'].lower() not in chosen_ids]
            selected=open_priority+chosen_early+rest[:max(0,remaining-early_take)]
            selected_keys={x['address'].lower() for x in selected}
            self.st['last_scan_universe']={'count':len(selected),'deep_batch':min(remaining,len(due)),'priority_batch':len(open_priority),'queue_size':len(q),'watch_size':len(self.st.get('daily_watch',[])),'updated_at':now}
            out=[]; telegram_messages=[]
            for x in selected:
                try:
                    key=x['address'].lower(); early=self.early_listing_signal(x) if early_cfg.get('enabled',True) and x.get('asset_class','crypto')=='crypto' else None
                    if early:
                        ew=self.st.setdefault('early_listing_watch',[])
                        ew.append({**early,'seen_at':now})
                        self.st['early_listing_watch']=ew[-100:]
                    snap=self.api.snapshot(x['name'],x['address'],include_4h=False)
                    # Deep technical analysis is performed on every selected rotation item.
                    try:
                        if x.get('asset_class')=='commodity':
                            rows=[]; tpool=snap.get('technical',{}).get('pool') if isinstance(snap.get('technical'),dict) else None
                        else:
                            rows,tpool=self.api.gecko_technical(x['address'],snap.get('pair_address'),snap.get('token_side'),x.get('chain_id') or CHAIN); rows=[r for r in rows if isinstance(r,(list,tuple)) and len(r)>=6]
                        if x.get('asset_class')=='commodity':
                            pass
                        else:
                            min_candles=int(self.cfg.get('score',{}).get('min_technical_candles',60) or 60)
                            snap['technical']=_technical_snapshot(rows,tpool,min_candles)
                            closes=[float(r[4]) for r in rows]
                            if len(closes)>=5: snap['change_4h']=(closes[-1]/closes[-5]-1)*100 if closes[-5]>0 else None
                    except Exception as e: snap['errors'].append('technical:'+str(e))
                    sig=score(snap,self.cfg); sig.update({k:snap.get(k) for k in ('price','market_cap','liquidity','volume_24h','change_1h','change_4h','holders','top10_pct','largest_holder_pct','buys','sells','technical','asset_class','chain_id')})
                    if snap.get('errors'): sig['data_errors']=snap['errors']
                    if early: sig['early_listing']=early
                    setup=sig.get('setup_type','UNKNOWN'); zone=sig.get('entry_zone'); price=float(sig.get('price') or 0); tech=sig.get('technical') if isinstance(sig.get('technical'),dict) else {}
                    in_zone=bool(zone and price and float(zone[0])<=price<=float(zone[1]))
                    # A RETEST is not entry-ready while price remains below resistance;
                    # a BREAKOUT must actually clear resistance; other setups need the
                    # normal zone plus their structural conditions.
                    if setup=='RETEST':
                        resistance=float(tech.get('resistance') or 0); sig['entry_ready']=bool(in_zone and resistance>0 and price>=resistance)
                    elif setup=='BREAKOUT':
                        sig['entry_ready']=bool(in_zone and float(tech.get('breakout_pct') or -999)>=0.15)
                    else:
                        sig['entry_ready']=bool(in_zone)
                    # Technical data is optional. Never let a missing/None technical
                    # snapshot abort the whole scan; this was the V5.8.2 crash path.
                    raw_tech=sig.get('technical')
                    tech=raw_tech if isinstance(raw_tech,dict) else {}
                    risk_cfg=self.cfg.get('risk') if isinstance(self.cfg.get('risk'),dict) else {}
                    atr_value=tech.get('atr_pct')
                    if atr_value is not None:
                        try:
                            sig['suggested_stop_pct']=max(2.0,min(15.0,float(atr_value)*float(risk_cfg.get('atr_stop_multiplier',1.5))))
                        except (TypeError,ValueError):
                            sig.setdefault('data_errors',[]).append('invalid_atr_pct')
                    min_tech=float(self.cfg.get('score',{}).get('min_tech_scan_score',0) or 0)
                    tech_score=sig.get('technical_score')
                    tech_ok=(x.get('asset_class')=='commodity' and sig.get('technical') is not None) or (tech_score is not None and float(tech_score)>=min_tech)
                    # A confirmation is counted only when the same pass has usable
                    # technical data and clears the configured technical threshold.
                    # Missing/429 technical data must never accumulate confirmations.
                    required=int(self.cfg['signal']['required_confirmations'])+(int(self.cfg['signal'].get('hot_extra_confirmations',1)) if 'HOT_MOMENTUM' in sig.get('risk_flags',[]) else 0); required=max(1,required); sig['required_confirmations']=required
                    conf=int(self.st['confirmations'].get(key,0) or 0)
                    # Confirmation is consecutive and capped at the required count.
                    # It must never grow to values such as 55/2 after a signal has
                    # remained strong for many scans. Any failed pass resets it.
                    conf=min(required,conf+1) if sig['verdict']=='BUY CANDIDATE' and tech_ok else 0
                    self.st['confirmations'][key]=conf; sig['confirmations']=conf
                    confirmed=False
                    if sig['verdict']=='BUY CANDIDATE' and conf>=required and tech_ok and sig.get('setup_type') not in ('MOMENTUM_CHASE','NO_SETUP') and sig.get('entry_ready'):
                        active=self.st.setdefault('signal_active',{}).get(key,False); last_score=float(self.st.setdefault('signal_last_score',{}).get(key,0) or 0); delta=abs(float(sig.get('score') or 0)-last_score); last=float(self.st.setdefault('last_alert',{}).get(key,0) or 0); repeat=float(self.cfg['signal'].get('alert_score_change_repeat',8));
                        cooldown=float(self.cfg['signal']['alert_cooldown_seconds'])
                        setup_changed=str(sig.get('setup_type','UNKNOWN')) != str(self.st.setdefault('signal_setup',{}).get(key,'UNKNOWN'))
                        materially_changed=(delta>=repeat or setup_changed or not active)
                        if now-last>=cooldown and materially_changed:
                            self.st['last_alert'][key]=now; confirmed=True
                        self.st.setdefault('signal_setup',{})[key]=str(sig.get('setup_type','UNKNOWN'))
                        self.st['signal_active'][key]=True; self.st['signal_last_score'][key]=float(sig.get('score') or 0)
                    else:
                        self.st.setdefault('signal_active',{})[key]=False
                    sig['signal']='CONFIRMED' if confirmed else None
                    tr=manage_position(self.st,self.cfg,sig)
                    if tr: sig['demo_exit']=tr; telegram_messages.append(f"📉 DEMO EXIT | {sig.get('name')} | {tr.get('reason')} | PnL ${tr.get('pnl',0):+.4f} ({tr.get('pnl_pct',0):+.2f}%)")
                    if confirmed and key not in self.st['demo']['positions']:
                        tid,why=risk_buy(self.st,self.cfg,sig,None); sig['demo_entry']=tid or why
                    # Early-listing lane: Demo-only automatic entry using dedicated fresh-pair filters.
                    if early and bool(early_cfg.get('auto_demo_buy',False)) and key not in self.st['demo']['positions']:
                        early_sig={**sig,'name':early['name'],'address':early['address'],'price':early['price'],
                                   'market_cap':early.get('market_cap'),'liquidity':early['liquidity'],
                                   'volume_24h':early.get('volume_5m'),'change_1h':early.get('change_5m'),
                                   'change_4h':0.0,'buys':early.get('buys_5m'),'sells':early.get('sells_5m'),
                                   'score':max(float(sig.get('score') or 0),float(early_cfg.get('demo_entry_score',70))),
                                   'verdict':'BUY CANDIDATE','setup_type':'EARLY_LISTING','entry_ready':True,
                                   'suggested_stop_pct':float(early_cfg.get('stop_loss_pct',10.0)),
                                   'risk_flags':['EARLY_LISTING']}
                        tid,why=risk_buy(self.st,self.cfg,early_sig,float(early_cfg.get('max_demo_entry_usd',5)))
                        sig['early_demo_entry']=tid or why
                        if tid: telegram_messages.append(self.format_signal_alert({**early_sig,'signal':'EARLY_DEMO_ENTRY','demo_entry':tid}))
                    if confirmed: telegram_messages.append(self.format_signal_alert(sig))
                    self.st.setdefault('last_scan',{})[key]={'name':sig.get('name'),'price':sig.get('price'),'ts':now,'score':sig.get('score'),'verdict':sig.get('verdict')}
                    if key in q:
                        q[key].update({'last_deep_scan':now,'next_deep_at':now+float(self.cfg['discovery'].get('deep_rescan_seconds',900)),'score':sig.get('score'),'setup_type':setup,'entry_ready':sig.get('entry_ready',False),'last_verdict':sig.get('verdict'),'last_price':sig.get('price')})
                        for dw in self.st.get('daily_watch',[]):
                            if str(dw.get('address','')).lower()==key:
                                dw.update({'score':sig.get('score'),'setup_type':setup,'entry_ready':sig.get('entry_ready',False),'verdict':sig.get('verdict'),'last_deep_scan':now,'next_deep_at':q[key].get('next_deep_at')}); break
                    out.append(sig); log.info('%s %s %.1f setup=%s entry=%s',sig['name'],sig['verdict'],sig['score'],setup,sig.get('entry_ready'))
                except Exception as e:
                    out.append({'name':x.get('name'),'address':x.get('address'),'verdict':'ERROR','error':str(e)}); log.exception('scan error')
            telegram_messages.extend(self._opportunity_messages(out, now))
            self.save()
            if telegram_messages:
                batch='\n\n'.join(telegram_messages)
                for i in range(0,len(batch),3800): self.send_telegram(batch[i:i+3800])
            return out
    def set_setting(self, key, value):
        f=self.cfg['filters']; r=self.cfg['risk']; sc=self.cfg['score']
        mapping={
            'mc_min':('filters','min_market_cap',float),'mc_max':('filters','max_market_cap',float),
            'liquidity':('filters','min_liquidity',float),'volume':('filters','min_volume_24h',float),'holders':('filters','min_holders',int),
            'top10_target':('filters','max_top10_pct',float),'top10_hard':('filters','top10_hard_limit',float),
            'largest_target':('filters','max_largest_holder_pct',float),'largest_hard':('filters','largest_holder_hard_limit',float),
            'buy_sell':('filters','min_buy_sell_ratio',float),'max_1h':('filters','max_1h_change',float),'min_4h':('filters','min_4h_change',float),
            'liq_mc':('filters','min_liquidity_mc_pct',float),'hot1':('filters','hot_1h_level_1',float),'hot2':('filters','hot_1h_level_2',float),'hot3':('filters','hot_1h_level_3',float),
            'watch_score':('score','min_watch',float),'buy_score':('score','min_buy_candidate',float),'tech_score':('score','min_tech_scan_score',float),'min_tech_candles':('score','min_technical_candles',int),
            'top10_penalty':('score','top10_excess_penalty_max',float),'largest_penalty':('score','largest_excess_penalty_max',float),'hot_penalty':('score','hot_1h_penalty_max',float),
            'confirm':('signal','required_confirmations',int),'hot_confirm':('signal','hot_extra_confirmations',int),'cooldown':('signal','entry_cooldown_minutes',float),'alert_cooldown':('signal','alert_cooldown_seconds',float),
            'demo_cap':('risk','demo_start_balance_usd',float),'max_trade':('risk','demo_max_per_trade_usd',float),'max_positions':('risk','max_open_positions',int),
            'max_trades':('risk','max_trades_per_day',int),'daily_loss':('risk','max_daily_loss_usd',float),'alloc_pct':('risk','allocation_per_trade_pct',float),'portfolio_pct':('risk','max_portfolio_allocation_pct',float),'sl':('risk','stop_loss_pct',float),'tp':('risk','take_profit_pct',float),'trailing':('risk','trailing_stop_pct',float),
            'early_max':('early_listing','max_demo_entry_usd',float),'early_liq':('early_listing','min_liquidity_usd',float),
            'early_vol5':('early_listing','min_volume_5m_usd',float),'early_buys5':('early_listing','min_buys_5m',int),'early_ratio5':('early_listing','min_buy_sell_ratio_5m',float),
            'early_ch5':('early_listing','max_price_change_5m',float),'early_mc_min':('early_listing','min_market_cap_usd',float),'early_mc_max':('early_listing','max_market_cap_usd',float),'early_age':('early_listing','candidate_max_age_minutes',float),
            'scan_max':('scanner','max_tokens_per_scan',int),'watch_size':('discovery','watchlist_size',int),'deep_rescan':('discovery','deep_rescan_seconds',float),'risk_pct':('risk','risk_per_trade_pct',float),'atr_mult':('risk','atr_stop_multiplier',float),'discover_max':('discovery','candidate_pool_per_scan',int),'deep_max':('discovery','deep_scan_candidates_per_scan',int),'early_reserve':('discovery','early_listing_reserve',int),'persistent_max':('discovery','max_persistent_watchlist',int),'alert_repeat_score':('signal','alert_score_change_repeat',float),
            'gecko_rate':('data','gecko_rate_limit_per_minute',int),'cache_ttl':('data','cache_ttl_seconds',float)
        }
        if key not in mapping: return 'Unknown setting'
        section,name,cast=mapping[key]; v=cast(value)
        if v < 0: raise ValueError('value must be >= 0')
        self.cfg[section][name]=v
        atomic_json(self.cfg_path,self.cfg)
        return f'{key}={v}'

    def live_status(self):
        until=float(self.st.get('live_armed_until',0) or 0)
        remaining=max(0,int(until-time.time()))
        return f"Live enabled={self.cfg.get('live',{}).get('enabled',False)} | armed={remaining>0} | remaining={remaining}s | panic={self.st.get('panic',False)}"

    def arm_live(self,code):
        live=self.cfg.get('live',{})
        if not live.get('enabled',False): return '🔐 Live در Config غیرفعال است.'
        if self.st.get('panic'): return '🛑 ابتدا PANIC STOP را Resume کنید.'
        secret=os.getenv('RH_TOTP_SECRET','').strip()
        if not secret: return 'خطا: RH_TOTP_SECRET موجود نیست.'
        if not totp_valid(secret,code): return '❌ کد TOTP نامعتبر است.'
        seconds=max(60,int(live.get('armed_seconds',300)))
        self.st['live_armed_until']=time.time()+seconds; self.save()
        return f'🔓 Live فقط برای {seconds} ثانیه Arm شد. اجرای واقعی همچنان به آداپتر قفل‌شده وابسته است.'

    def telegram_menu(self):
        return {
            'inline_keyboard': [
                [{'text':'📊 اسکن بازار', 'callback_data':'scan'}, {'text':'🎯 فرصت‌ها', 'callback_data':'opportunities'}],
                [{'text':'🔎 بررسی توکن', 'callback_data':'scan_token'}, {'text':'🔍 Discovery', 'callback_data':'discover'}],
                [{'text':'💰 Demo', 'callback_data':'demo'}, {'text':'📦 پوزیشن‌ها', 'callback_data':'positions'}],
                [{'text':'👀 Watchlist', 'callback_data':'watchlist'}, {'text':'📈 گزارش‌ها', 'callback_data':'reports'}],
                [{'text':'🛡 ریسک', 'callback_data':'risk'}, {'text':'⚙️ تنظیمات', 'callback_data':'settings'}],
                [{'text':'🔐 Live', 'callback_data':'live'}, {'text':'🛑 PANIC STOP', 'callback_data':'panic'}],
            ]
        }

    def telegram_submenu(self, kind):
        if kind == 'demo':
            return {'inline_keyboard': [
                [{'text':'💰 وضعیت Demo', 'callback_data':'demo_status'}, {'text':'📦 پوزیشن‌ها', 'callback_data':'positions'}],
                [{'text':'♻️ Reset Demo', 'callback_data':'demo_reset'}, {'text':'🔙 منوی اصلی', 'callback_data':'menu'}],
            ]}
        if kind == 'reports':
            return {'inline_keyboard': [
                [{'text':'📅 امروز', 'callback_data':'report_today'}, {'text':'7️⃣ هفته', 'callback_data':'report_weekly'}],
                [{'text':'3️⃣0️⃣ ماه', 'callback_data':'report_monthly'}, {'text':'♾️ کل', 'callback_data':'report_all'}],
                [{'text':'🚀 Early Listings', 'callback_data':'report_early'}, {'text':'🎯 فرصت‌ها', 'callback_data':'opportunities'}],
                [{'text':'🔙 منوی اصلی', 'callback_data':'menu'}],
            ]}
        if kind == 'settings':
            return {'inline_keyboard': [
                [{'text':'⚙️ مشاهده تنظیمات', 'callback_data':'settings_view'}, {'text':'🛠 تغییر با /set', 'callback_data':'settings_help'}],
                [{'text':'🔙 منوی اصلی', 'callback_data':'menu'}],
            ]}
        if kind == 'watchlist':
            return {'inline_keyboard': [
                [{'text':'🔄 بروزرسانی', 'callback_data':'watchlist'}, {'text':'➕ افزودن /watch', 'callback_data':'watch_help'}],
                [{'text':'🔙 منوی اصلی', 'callback_data':'menu'}],
            ]}
        return {'inline_keyboard': [[{'text':'🔙 منوی اصلی', 'callback_data':'menu'}]]}

    def telegram_send(self, chat, text, keyboard=None):
        token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
        if not token or not chat: return False
        payload={'chat_id':chat,'text':str(text)[:3900]}
        if keyboard: payload['reply_markup']=json.dumps(keyboard,ensure_ascii=False)
        try:
            r=self.api.s.post(TG.format(token,'sendMessage'),json=payload,timeout=15)
            r.raise_for_status(); return True
        except Exception as e:
            log.warning('Telegram send failed: %s',e); return False

    def telegram_edit(self, chat, message_id, text, keyboard=None):
        token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
        if not token or not chat or not message_id: return False
        payload={'chat_id':chat,'message_id':message_id,'text':str(text)[:3900]}
        if keyboard: payload['reply_markup']=json.dumps(keyboard,ensure_ascii=False)
        try:
            r=self.api.s.post(TG.format(token,'editMessageText'),json=payload,timeout=15)
            r.raise_for_status(); return True
        except Exception as e:
            log.warning('Telegram edit failed: %s',e); return False

    def telegram_answer(self, callback_id, text=''):
        token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
        if not token or not callback_id: return
        try:
            self.api.s.post(TG.format(token,'answerCallbackQuery'),json={'callback_query_id':callback_id,'text':str(text)[:180]},timeout=10)
        except Exception:
            pass

    def demo_text(self):
        d=self.st['demo']
        return (f"💰 DEMO\nCash: ${d['cash']:.2f}\nRealized P/L: ${d['realized_pnl']:+.2f}\n"
                f"Open positions: {len(d.get('positions',{}))}\nToday loss: ${self.st['today']['loss']:.2f}\n"
                f"Trades today: {self.st['today']['trades']}")

    def telegram_status_text(self):
        d=self.st['demo']; positions=self.st['demo'].get('positions',{})
        return (f"🤖 Global Multi-Asset Bot V5.10.1\n\n"
                f"Mode: DEMO\n"
                f"Scanner: every {self.cfg['scanner']['interval_seconds']}s\n"
                f"Last scan universe: {self.st.get('last_scan_universe',{}).get('count',0)} token(s)\n"
                f"Demo cash: ${d['cash']:.2f}\n"
                f"Open positions: {len(positions)}\n"
                f"Realized P/L: ${d['realized_pnl']:+.2f}\n"
                f"Today loss: ${self.st['today']['loss']:.2f}\n"
                f"Trades today: {self.st['today']['trades']}\n"
                f"PANIC: {'ON 🛑' if self.st.get('panic') else 'OFF'}\n"
                f"Live: {'ENABLED' if self.cfg.get('live',{}).get('enabled') else 'DISABLED'}")

    def telegram_watchlist_text(self):
        configured=self.cfg.get('watchlist',[]) if isinstance(self.cfg.get('watchlist',[]),list) else []
        manual=self.st.get('manual_watchlist',[]) if isinstance(self.st.get('manual_watchlist',[]),list) else []
        lines=['👀 WATCHLIST','',f'📌 ثابت: {len(configured)}']
        for x in configured:
            lines.append(f"• {x.get('name','TOKEN')} — {x.get('address','')}")
        lines.append(f'\n📝 دستی: {len(manual)}')
        for x in manual:
            lines.append(f"• {x.get('name','TOKEN')} — {x.get('address','')}")
        daily=self.st.get('daily_watch',[])
        lines.append(f'\n⭐ واچ امروز: {len(daily)}')
        for x in daily[:20]:
            lines.append(f"• {x.get('name','TOKEN')} — {x.get('verdict','?')} — {x.get('score','?')}")
        return '\n'.join(lines)[:3900]

    def telegram_positions_text(self):
        positions=self.st['demo'].get('positions',{})
        if not positions: return '📦 پوزیشن باز Demo نداریم.'
        lines=['📦 DEMO POSITIONS','']
        for a,p in positions.items():
            last=self.st.get('last_scan',{}).get(a,{})
            price=float(last.get('price') or p.get('entry') or 0)
            value=float(p.get('qty',0))*price
            pnl=value-float(p.get('invested',0))
            lines.append(f"• {p.get('name','TOKEN')} | Entry ${float(p.get('entry',0)):.8g} | Now ${price:.8g} | P/L ${pnl:+.4f}")
        return '\n'.join(lines)

    def telegram_help_text(self):
        return ('🆘 فرمان‌های اصلی\n\n'
                '/start یا /menu — منوی اصلی\n'
                '/scan — اسکن کامل\n'
                '/discover — Discovery\n'
                '/opportunities — بهترین فرصت‌های فعلی\n'
                '/risk — وضعیت ریسک و سرمایه\n'
                '/watchlist — واچ‌لیست\n'
                '/scan_token SYMBOL ADDRESS — بررسی یک قرارداد\n'
                '/watch SYMBOL ADDRESS — افزودن به واچ دستی\n'
                '/unwatch SYMBOL یا ADDRESS — حذف\n'
                '/demo — وضعیت Demo\n'
                '/demo_balance AMOUNT — تنظیم موجودی و ریست Demo\n'
                '/demo_reset — ریست Demo\n'
                '/today /daily /weekly /monthly /report_all — گزارش عملکرد\n'
                '/settings — تنظیمات\n'
                '/set KEY VALUE — تغییر تنظیم قابل‌پشتیبانی\n'
                '/panic /resume — توقف/ادامه\n'
                '/early — گزارش شکار لیست‌های تازه\n/live /arm CODE — وضعیت و ARM کردن Live')

    def telegram_text(self,txt):
        txt=txt.strip()
        if txt in ('/start','/menu'):
            return '🤖 Global Multi-Asset Bot V5.10.1\n\nپنل کنترل آماده است. از دکمه‌های زیر استفاده کن.'
        if txt=='/help': return self.telegram_help_text()
        if txt=='/status': return self.telegram_status_text()
        if txt=='/demo': return f"{self.demo_text()}\n\n{self.telegram_positions_text()}"
        if txt=='/positions': return self.telegram_positions_text()
        if txt=='/watchlist': return self.telegram_watchlist_text()
        if txt=='/live': return self.live_status()
        if txt.startswith('/arm '): return self.arm_live(txt.split(maxsplit=1)[1])
        if txt.startswith('/demo_balance '):
            try:
                amount=float(txt.split(maxsplit=1)[1])
                if amount<0 or not math.isfinite(amount): raise ValueError('amount must be >= 0')
                self.st['demo']={'cash':amount,'positions':{},'realized_pnl':0.0,'trades':[]}; self.st['today']={'date':datetime.now(timezone.utc).date().isoformat(),'loss':0.0,'trades':0}; self.st['confirmations']={}; self.st['last_alert']={}; self.st['signal_active']={}; self.st['signal_last_score']={}; self.st['entry_cooldowns']={}; self.save(); return f'✅ Demo balance=${amount:.2f} و Demo ریست شد.'
            except Exception as e: return 'خطا: '+str(e)
        if txt=='/demo_reset':
            self.st['demo']={'cash':float(self.cfg['risk']['demo_start_balance_usd']),'positions':{},'realized_pnl':0.0,'trades':[]}; self.st['today']={'date':datetime.now(timezone.utc).date().isoformat(),'loss':0.0,'trades':0}; self.st['confirmations']={}; self.st['last_alert']={}; self.st['signal_active']={}; self.st['signal_last_score']={}; self.st['entry_cooldowns']={}; self.save(); return '♻️ Demo reset شد.'
        if txt=='/today': return '\n'.join(f"• {x.get('name')} — {x.get('verdict','?')} — {x.get('score','?')}/100" for x in self.st.get('daily_watch',[]))[:3900] or '👀 واچ امروز خالی است.'
        if txt=='/daily': return '📅 امروز\n'+json.dumps(report(self.st,1),ensure_ascii=False)
        if txt=='/weekly': return '📊 هفته\n'+json.dumps(report(self.st,7),ensure_ascii=False)
        if txt=='/monthly': return '📈 ماه\n'+json.dumps(report(self.st,30),ensure_ascii=False)
        if txt=='/report_all': return '📊 کل سابقه\n'+json.dumps(report(self.st,None),ensure_ascii=False)
        if txt=='/early': return early_listing_report(self.st)
        if txt=='/panic': self.st['panic']=True; self.st['live_armed_until']=0; self.save(); return '🛑 PANIC STOP فعال شد.'
        if txt=='/resume': self.st['panic']=False; self.save(); return '✅ PANIC STOP خاموش شد. Live همچنان قفل است.'
        if txt=='/discover':
            found,_=self.api.discover(int(self.cfg.get('discovery',{}).get('candidate_pool_per_scan',40)))
            if not found: return '🔍 Discovery موردی پیدا نکرد.'
            lines=['🔍 DISCOVERY',f'Candidates: {len(found)}','']
            lines.extend(f"• {x.get('name','TOKEN')} | {x.get('chain_id','')} | {x.get('address','')}" for x in found[:35])
            return '\n'.join(lines)[:3900]
        if txt=='/opportunities':
            xs=[]
            for x in self.st.get('daily_watch',[]):
                if x.get('verdict') in ('BUY CANDIDATE','WATCH'):
                    xs.append(x)
            xs.sort(key=lambda x:float(x.get('score') or 0),reverse=True)
            if not xs: return '🎯 فعلاً Opportunity قابل‌نمایشی نداریم.'
            lines=['🎯 OPPORTUNITIES','']
            for x in xs[:12]:
                lines.append(f"• {x.get('name','TOKEN')} | {x.get('verdict','?')} | {float(x.get('score') or 0):.1f} | {x.get('setup_type','UNKNOWN')} | Entry={'YES' if x.get('entry_ready') else 'NO'}")
            return '\n'.join(lines)[:3900]
        if txt=='/risk':
            r=self.cfg.get('risk',{}); d=self.st.get('demo',{})
            equity=_portfolio_equity(self.st); invested=sum(float(p.get('invested',0) or 0) for p in d.get('positions',{}).values())
            return (f"🛡 RISK\nEquity: ${equity:.2f}\nCash: ${float(d.get('cash',0)):.2f}\n"
                    f"Invested: ${invested:.2f}/{equity*float(r.get('max_portfolio_allocation_pct',75))/100:.2f}\n"
                    f"Risk/trade: {r.get('risk_per_trade_pct',2)}% | Max positions: {r.get('max_open_positions',3)}\n"
                    f"Daily loss: ${self.st.get('today',{}).get('loss',0):.2f}/${r.get('max_daily_loss_usd',2):.2f}\n"
                    f"LIVE: {'DISABLED' if not self.cfg.get('live',{}).get('enabled') else 'ENABLED'}")
        if txt.startswith('/watch '):
            parts=txt.split()
            if len(parts)!=3 or not ADDR_RE.match(parts[2]): return 'استفاده: /watch SYMBOL 0xCONTRACT_ADDRESS'
            a=parts[2]; manual=self.st.setdefault('manual_watchlist',[])
            if any(str(x.get('address','')).lower()==a.lower() for x in manual): return 'این توکن قبلاً در Manual Watchlist است.'
            cap=int(self.cfg.get('discovery',{}).get('manual_watchlist_max',20))
            if len(manual)>=cap: return f'Manual Watchlist پر است (حداکثر {cap}).'
            manual.append({'name':parts[1].upper(),'address':a}); self.save(); return f'✅ {parts[1].upper()} به Manual Watchlist اضافه شد.'
        if txt.startswith('/unwatch '):
            key=txt.split(maxsplit=1)[1].strip().lower(); manual=self.st.setdefault('manual_watchlist',[])
            before=len(manual); self.st['manual_watchlist']=[x for x in manual if str(x.get('address','')).lower()!=key and str(x.get('name','')).lower()!=key]; self.save(); return '✅ حذف شد.' if len(self.st['manual_watchlist'])<before else 'در Manual Watchlist پیدا نشد.'
        if txt.startswith('/scan_token '):
            parts=txt.split()
            if len(parts)!=3 or not ADDR_RE.match(parts[2]): return 'استفاده: /scan_token SYMBOL 0xCONTRACT_ADDRESS'
            old=list(self.st.get('manual_watchlist',[]))
            try:
                self.st['manual_watchlist']=[{'name':parts[1].upper(),'address':parts[2]}]
                result=self.scan()
                return json.dumps(result[0] if result else {'error':'no result'},ensure_ascii=False,indent=2)[:3900]
            finally:
                self.st['manual_watchlist']=old; self.save()
        if txt=='/settings':
            f=self.cfg['filters']; r=self.cfg['risk']; sc=self.cfg['score']; sg=self.cfg['signal']; return (f"⚙️ SETTINGS V5.10.1\nMC ${f['min_market_cap']:,.0f}-${f['max_market_cap']:,.0f} | Liq ${f['min_liquidity']:,.0f} | Vol ${f['min_volume_24h']:,.0f} | Holders {f['min_holders']}\n"
                f"Top10 {f['max_top10_pct']}%/{f.get('top10_hard_limit')}% | Largest {f['max_largest_holder_pct']}%/{f.get('largest_holder_hard_limit')}%\n"
                f"Buy/Sell {f['min_buy_sell_ratio']} | 1H {f['max_1h_change']}% | 4H {f['min_4h_change']}% | Liq/MC {f['min_liquidity_mc_pct']}%\n"
                f"Score Watch/Buy {sc['min_watch']}/{sc['min_buy_candidate']} | Tech {sc.get('min_tech_scan_score')} | Confirm {sg['required_confirmations']} | Entry cooldown {sg.get('entry_cooldown_minutes',0)}m\n"
                f"Demo ${r['demo_start_balance_usd']} | target/trade {r.get('allocation_per_trade_pct',25)}% | max/trade ${r['demo_max_per_trade_usd']} | portfolio {r.get('max_portfolio_allocation_pct',75)}% | risk/trade {r.get('risk_per_trade_pct',2)}%\n"
                f"SL {r['stop_loss_pct']}% | TP {r['take_profit_pct']}% | Trail {r['trailing_stop_pct']}%\n"
                f"Rotation: feed {self.cfg['discovery'].get('candidate_pool_per_scan',40)} | watch {self.cfg['discovery'].get('watchlist_size',50)} | deep/batch {self.cfg['discovery'].get('deep_scan_candidates_per_scan',12)} | rescan {self.cfg['discovery'].get('deep_rescan_seconds',900)}s | Manual max {self.cfg['discovery'].get('manual_watchlist_max',20)}\n"
                f"Live enabled={self.cfg['live']['enabled']}")
        if txt.startswith('/set '):
            parts=txt.split()
            if len(parts)!=3: return 'استفاده: /set KEY VALUE — کلیدها را در /settings ببین.'
            try: return '✅ OK: '+self.set_setting(parts[1],parts[2])
            except Exception as e: return 'خطا: '+str(e)
        if txt=='/scan':
            return '⏳ برای اجرای اسکن از دکمه 📊 اسکن بازار یا /scan استفاده کن.'
        return 'دستور نامعتبر. /menu'

    def telegram_callback(self, callback):
        self.telegram_answer(callback.get('id'))
        msg=callback.get('message') or {}; chat=str((msg.get('chat') or {}).get('id','')); mid=msg.get('message_id'); data=callback.get('data','')
        if not chat or not self.allowed_chat(chat): return
        if data=='menu': self.telegram_edit(chat,mid,self.telegram_text('/start'),self.telegram_menu()); return
        if data=='scan':
            self.telegram_edit(chat,mid,'⏳ اسکن در حال انجام است...',self.telegram_menu())
            try:
                results=self.scan(); text='📊 SCAN COMPLETE\n\n'+'\n\n'.join(f"• {x.get('name')} — {x.get('verdict')} — {x.get('score','?')}/100" for x in results)
            except Exception as e: text='❌ خطا در اسکن: '+str(e)
            self.telegram_send(chat,text,self.telegram_menu()); return
        if data=='scan_token': self.telegram_send(chat,'🔎 بررسی یک توکن:\n/scan_token SYMBOL 0xCONTRACT_ADDRESS',self.telegram_menu()); return
        if data=='discover':
            self.telegram_send(chat,self.telegram_text('/discover'),self.telegram_menu()); return
        if data=='opportunities':
            self.telegram_send(chat,self.telegram_text('/opportunities'),self.telegram_menu()); return
        if data=='risk':
            self.telegram_send(chat,self.telegram_text('/risk'),self.telegram_menu()); return
        if data=='watchlist': self.telegram_send(chat,self.telegram_watchlist_text(),self.telegram_submenu('watchlist')); return
        if data=='watch_help': self.telegram_send(chat,'➕ افزودن به واچ دستی:\n/watch SYMBOL 0xCONTRACT_ADDRESS',self.telegram_submenu('watchlist')); return
        if data=='demo': self.telegram_send(chat,self.telegram_text('/demo'),self.telegram_submenu('demo')); return
        if data=='demo_status': self.telegram_send(chat,self.telegram_text('/demo'),self.telegram_submenu('demo')); return
        if data=='positions': self.telegram_send(chat,self.telegram_positions_text(),self.telegram_submenu('demo')); return
        if data=='demo_reset': self.telegram_send(chat,self.telegram_text('/demo_reset'),self.telegram_submenu('demo')); return
        if data=='reports': self.telegram_send(chat,'📈 گزارش‌ها را انتخاب کن:',self.telegram_submenu('reports')); return
        if data=='report_today': self.telegram_send(chat,self.telegram_text('/daily'),self.telegram_submenu('reports')); return
        if data=='report_weekly': self.telegram_send(chat,self.telegram_text('/weekly'),self.telegram_submenu('reports')); return
        if data=='report_monthly': self.telegram_send(chat,self.telegram_text('/monthly'),self.telegram_submenu('reports')); return
        if data=='report_all': self.telegram_send(chat,self.telegram_text('/report_all'),self.telegram_submenu('reports')); return
        if data=='report_early': self.telegram_send(chat,early_listing_report(self.st),self.telegram_submenu('reports')); return
        if data=='settings': self.telegram_send(chat,'⚙️ تنظیمات:',self.telegram_submenu('settings')); return
        if data=='settings_view': self.telegram_send(chat,self.telegram_text('/settings'),self.telegram_submenu('settings')); return
        if data=='settings_help': self.telegram_send(chat,'🛠 برای تغییر تنظیمات:\n/set KEY VALUE\n\nکلیدهای قابل تغییر در /settings و مستندات پروژه هستند.',self.telegram_submenu('settings')); return
        if data=='live': self.telegram_send(chat,'🔐 '+self.live_status()+'\n\nبرای ARM:\n/arm 123456',self.telegram_menu()); return
        if data=='panic': self.st['panic']=True; self.st['live_armed_until']=0; self.save(); self.telegram_send(chat,'🛑 PANIC STOP فعال شد.',self.telegram_menu()); return

    def telegram_poll_once(self, max_updates=10):
        """Process a bounded batch of Telegram updates for scheduled runners.
        This keeps the public GitHub Actions runner usable without a permanent
        polling process. State offset is persisted between runs.
        """
        token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
        if not token or not self.cfg.get('telegram',{}).get('enabled',True): return 0
        processed=0
        try:
            x=self.api.s.get(TG.format(token,'getUpdates'),params={'timeout':1,'offset':self.st.get('telegram_offset',0),'allowed_updates':json.dumps(['message','callback_query'])},timeout=8).json()
            if not x.get('ok',True): return 0
            for u in (x.get('result') or [])[:max(1,int(max_updates))]:
                self.st['telegram_offset']=u['update_id']+1; processed+=1
                if u.get('callback_query'):
                    self.telegram_callback(u['callback_query']); continue
                m=u.get('message') or {}; chat=m.get('chat',{}).get('id'); text=(m.get('text') or '').strip()
                if not chat or not self.allowed_chat(chat): continue
                if text=='/scan':
                    self.telegram_send(chat,'⏳ اسکن در حال انجام است...',self.telegram_menu())
                    try:
                        results=self.scan(); msg='📊 SCAN COMPLETE\\n\\n'+'\\n\\n'.join(f"• {r.get('name')} — {r.get('verdict')} — {r.get('score','?')}/100" for r in results)
                    except Exception as e: msg='❌ خطا در اسکن: '+str(e)
                    self.telegram_send(chat,msg,self.telegram_menu())
                else:
                    msg=self.telegram_text(text)
                    keyboard=self.telegram_menu() if text in ('/start','/menu','/status','/help','/panic','/resume') else None
                    self.telegram_send(chat,msg,keyboard)
            self.save()
        except Exception as e:
            log.warning('telegram poll once: %s',e)
        return processed

    def telegram_loop(self):
        token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
        if not token: raise RuntimeError('TELEGRAM_BOT_TOKEN missing')
        self.telegram_send(self.telegram_targets()[0] if self.telegram_targets() else None,self.telegram_text('/start'),self.telegram_menu())
        while True:
            self.telegram_poll_once(max_updates=20)
            time.sleep(1)

def main():
    import argparse
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default='config.json'); ap.add_argument('--state',default='state.json'); ap.add_argument('--once',action='store_true'); ap.add_argument('--telegram',action='store_true'); ap.add_argument('--self-test',action='store_true'); args=ap.parse_args()
    if args.self_test: print('Use 47_TESTS.py'); return
    b=Bot(args.config,args.state)
    if args.telegram: b.telegram_loop(); return
    if args.once:
        print(json.dumps(b.scan(),ensure_ascii=False,indent=2))
        b.telegram_poll_once(max_updates=10)
        return
    while True:
        b.scan(); time.sleep(int(b.cfg['scanner']['interval_seconds']))

if __name__=='__main__': main()
