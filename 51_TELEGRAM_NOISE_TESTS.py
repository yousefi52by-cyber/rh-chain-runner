#!/usr/bin/env python3
"""Post-patch Telegram anti-spam regression checks."""
import importlib.util, json, tempfile
from pathlib import Path
SRC=Path('03_rh_chain_bot_v4.py')
spec=importlib.util.spec_from_file_location('rhbot',SRC); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as d:
    cfg=m.load_json('01_config.example.json',{})
    cfg['telegram']={'enabled':False,'admin_chat_ids':[]}
    cfg['signal']['telegram_opportunity_alerts']=False
    cfg['watchlist']=[]
    cp=Path(d)/'config.json'; sp=Path(d)/'state.json'; cp.write_text(json.dumps(cfg))
    b=m.Bot(str(cp),str(sp))
    base={'name':'TEST','address':'0x'+'1'*40,'price':1.0,'verdict':'BUY CANDIDATE','score':70,'technical_score':80,'setup_type':'PULLBACK','entry_ready':False,'risk_flags':[]}
    assert b._opportunity_messages([base],1000)==[], 'opportunity alerts must be opt-in'
    cfg['signal']['telegram_opportunity_alerts']=True; cp.write_text(json.dumps(cfg)); b=m.Bot(str(cp),str(sp))
    assert len(b._opportunity_messages([base],1000))==1
    changed=dict(base,score=76)
    assert b._opportunity_messages([changed],1120)==[], 'score changes must not bypass cooldown'
print('V5.10.2 TELEGRAM ANTI-SPAM TESTS OK')
