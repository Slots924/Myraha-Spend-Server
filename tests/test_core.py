import copy
import json
from datetime import datetime, timezone, date, timedelta
from uuid import uuid4
import pytest
from app.credentials import ingest, candidates
from app.models import Ingest, ProxyInput
from app.db import DEFAULTS, now
from app.spend import micros, with_commission, split_even, persist_insights, account_window, oldest_day, prepare_exports, collect, export
from app.integrations import parse_proxy, save_proxy, RemoteError, Meta, request_json, Keitaro
from app.worker import enqueue, recover, Worker


def payload(token='EAA-test-token-123456789', iid=None, sent=None):
    return dict(schemaVersion=3, installationId=iid or str(uuid4()), sentAt=sent or datetime.now(timezone.utc).isoformat(),
                enabled=True, token={'value':token}, cookies={'items':[{'name':'xs','value':'secret-cookie','domain':'.facebook.com'}]},userAgent={'value':'Mozilla/5.0 test-UA'})


def account(db, aid='123', tz='Europe/Kyiv'):
    db.execute('INSERT INTO meta_ad_accounts(id,name,currency,timezone) VALUES(?,?,?,?)',(aid,'Ads','USD',tz))
    return db.one('SELECT * FROM meta_ad_accounts WHERE id=?',(aid,))


def insight(day='2026-09-05', spend='10.20', cid='456'):
    return dict(campaign_id=cid,campaign_name='Campaign',date_start=day,date_stop=day,spend=spend,account_currency='USD')


def test_ingest_idempotency_rotation_and_encryption(db):
    p=payload();ingest(db,Ingest(**p));ingest(db,Ingest(**p))
    assert len(db.rows('SELECT * FROM api_clients'))==1
    assert len(db.rows('SELECT * FROM installations'))==1
    stored=db.one('SELECT * FROM api_clients')
    assert 'EAA-test' not in stored['token'] and 'secret-cookie' not in stored['cookies']
    db.execute("UPDATE api_clients SET status='inactive'")
    p['sentAt']=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat();p['cookies']['items']=[]
    p['userAgent']['value']='Changed UA'
    ingest(db,Ingest(**p))
    assert db.one('SELECT status FROM api_clients')['status']=='unknown'
    assert db.decrypt(db.one('SELECT cookies FROM api_clients')['cookies'])=='[]'
    other=copy.deepcopy(p);other['installationId']=str(uuid4());ingest(db,Ingest(**other))
    assert len(db.rows('SELECT * FROM api_clients'))==1
    p['token']['value']='another-token';p['sentAt']=(datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat()
    ingest(db,Ingest(**p));assert len(db.rows('SELECT * FROM api_clients'))==2
    assert len(db.rows('SELECT * FROM installations'))==2


def test_null_v2_and_out_of_order(db):
    p=payload();ingest(db,Ingest(**p))
    old=copy.deepcopy(p);old['sentAt']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat();old['userAgent']['value']='stale';ingest(db,Ingest(**old))
    p['schemaVersion']=2;p.pop('userAgent');p['token']['value']=None;p['sentAt']=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
    ingest(db,Ingest(**p));assert db.one('SELECT user_agent FROM api_clients')['user_agent']=='Mozilla/5.0 test-UA'
    assert len(candidates(db))==1
    p['enabled']=False;p['sentAt']=(datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat();ingest(db,Ingest(**p));assert candidates(db)==[]


def test_exact_money_and_dates():
    assert micros('10.20')+micros('5.10')==15300000
    assert with_commission(micros('15.30'),'10')==16830000
    parts=[split_even(11220001,3,i) for i in range(3)]
    assert parts==[3740001,3740000,3740000] and sum(parts)==11220001
    s={**DEFAULTS,'earliest_date':'2020-01-01'}
    assert oldest_day(date(2026,4,30),s)==date(2026,2,28)
    a={'timezone':'America/Los_Angeles','last_collected_day':'2026-09-02'}
    start,end=account_window(a,DEFAULTS,datetime(2026,9,20,1,tzinfo=timezone.utc))
    assert (start,end)==(date(2026,9,2),date(2026,9,19))


def test_snapshots_corrections_zero_and_dst(db):
    a=account(db,tz='America/New_York');dt=datetime(2026,9,5,15,3,tzinfo=timezone.utc)
    persist_insights(db,a,[insight()],date(2026,9,5),date(2026,9,5),1,dt)
    persist_insights(db,a,[insight(spend='12.30')],date(2026,9,5),date(2026,9,5),1,dt+timedelta(hours=1))
    last=db.rows('SELECT * FROM spend_snapshots ORDER BY id')[-1]
    assert last['previous_micros']==10200000 and last['delta_micros']==2100000
    assert '16:03:00' in last['bucket_at']
    persist_insights(db,a,[],date(2026,9,5),date(2026,9,5),1,dt+timedelta(hours=2))
    assert db.one('SELECT spend_micros FROM spend_latest')['spend_micros']==0
    persist_insights(db,a,[insight(day='2026-11-01')],date(2026,11,1),date(2026,11,1),1,dt)
    r=db.one("SELECT * FROM spend_latest WHERE day='2026-11-01'")
    assert (datetime.fromisoformat(r['period_end'])-datetime.fromisoformat(r['period_start'])).total_seconds()==25*3600


def test_partial_or_invalid_response_never_zeroes_data(db):
    a=account(db);dt=datetime.now(timezone.utc)
    persist_insights(db,a,[insight()],date(2026,9,5),date(2026,9,5),1,dt)
    with pytest.raises(ValueError):
        persist_insights(db,a,[insight(spend='NaN')],date(2026,9,5),date(2026,9,5),1,dt)
    assert db.one('SELECT spend_micros FROM spend_latest')['spend_micros']==10200000


def test_proxy_parser_and_encoding(db):
    p=parse_proxy('http://host.example:15169:user:pass:word[https://provider.example/changeip/key]')
    assert p['password']=='pass:word' and p['port']==15169
    p2=parse_proxy('socks5://hello:p%40ss@host.example:1234')
    assert p2['password']=='p@ss' and p2['protocol']=='socks5h'
    pid=save_proxy(db,ProxyInput(**p));assert db.one('SELECT password FROM proxies WHERE id=?',(pid,))['password']!='pass:word'


def test_job_uniqueness_recovery_manual_force(db,config):
    a=enqueue(db,'spend-collect');assert enqueue(db,'spend-collect')==a
    enqueue(db,'spend-collect',full=True,force=True)
    row=db.one('SELECT * FROM jobs');assert row['full_scan']==1 and row['force_export']==1
    db.execute("UPDATE jobs SET status='running'");recover(db)
    assert db.one('SELECT status FROM jobs')['status']=='queued'


def seed_proxy(db):
    pid=save_proxy(db,ProxyInput(host='proxy.example',port=8080));db.execute('UPDATE proxies SET is_primary=1 WHERE id=?',(pid,))


def test_collect_dedup_fallback_and_transport_not_inactive(db,config):
    seed_proxy(db)
    for token in ['token-A','token-B','token-C']:
        ingest(db,Ingest(**payload(token)))
    calls=[]
    class FakeMeta:
        def __init__(self,db,cfg,client,proxy):self.id=client['id']
        def accounts(self):
            if self.id==3:raise RemoteError('Network error',transport=True)
            return [dict(id='act_123',account_id='123',name='Ads',currency='USD',timezone_name='UTC')]
        def insights(self,aid,start,end):
            calls.append(self.id)
            if self.id==1:raise RemoteError('Meta error 190',invalid_client=True)
            return [insight(day=str(end))]
        def close(self):pass
    status,_=collect(db,config,{'id':1,'full_scan':0},FakeMeta,lambda *_:None)
    assert status=='warning' and calls==[1,2]
    assert db.one('SELECT status FROM api_clients WHERE id=1')['status']=='inactive'
    assert db.one('SELECT status FROM api_clients WHERE id=3')['status']=='unknown'
    assert len(db.rows('SELECT * FROM spend_latest'))==1


def test_export_idempotency_failure_restart_and_commission(db,config):
    today=datetime.now(timezone.utc).date();db.settings_update({'earliest_date':str(today-timedelta(days=30))})
    a=account(db,tz='UTC');persist_insights(db,a,[insight(day=str(today))],today,today,1,datetime.now(timezone.utc))
    calls=[]
    class FakeKeitaro:
        fail=True
        def __init__(self,cfg):pass
        def mappings(self,*args):return [('456',12),('456',13)]
        def update_costs(self,row):
            calls.append(row.copy())
            if row['keitaro_campaign_id']==13 and self.fail:raise RemoteError('HTTP 503')
        def close(self):pass
    job={'id':1,'force_export':1}
    status,_=export(db,config,job,FakeKeitaro);assert status=='warning'
    assert all(r['spend_micros']==5610000 for r in calls)
    assert sum(r['spend_micros'] for r in calls)==11220000
    assert db.one("SELECT count(*) n FROM keitaro_cost_exports WHERE status='failed'")['n']==1
    FakeKeitaro.fail=False;export(db,config,job,FakeKeitaro)
    assert len(db.rows('SELECT * FROM keitaro_cost_exports'))==2
    assert db.one("SELECT count(*) n FROM keitaro_cost_exports WHERE status='sent'")['n']==2
    count=len(calls);export(db,config,{'id':1},FakeKeitaro);assert len(calls)==count
    db.settings_update({'commission_percent':'20'});export(db,config,job,FakeKeitaro)
    assert {r['spend_micros'] for r in calls[-2:]}=={6120000} and sum(r['spend_micros'] for r in calls[-2:])==12240000
    db.execute("UPDATE keitaro_cost_exports SET status='sending' WHERE id=1");recover(db)
    assert db.one('SELECT status FROM keitaro_cost_exports WHERE id=1')['status']=='failed'


def test_meta_error_classification_and_safe_message(monkeypatch):
    class Response:
        status_code=400
        def json(self):return {'error':{'code':190,'message':'EAA-SECRET-TOKEN'}}
    class Session:
        def request(self,*a,**k):return Response()
    with pytest.raises(RemoteError) as info:request_json(Session(),'GET','https://graph.facebook.com',meta=True)
    assert info.value.invalid_client and 'SECRET' not in str(info.value)
    Response.json=lambda _: {'error':{'code':4,'message':'rate limited'}}
    monkeypatch.setattr('app.integrations.time.sleep',lambda _:None)
    with pytest.raises(RemoteError) as info:request_json(Session(),'GET','https://graph.facebook.com',meta=True)
    assert not info.value.invalid_client


def test_meta_pagination_stays_on_fixed_host():
    meta=Meta.__new__(Meta);calls=[]
    def get(path,params):
        calls.append((path,params.copy()))
        return {'data':[1],'paging':{'next':'https://evil.example/steal','cursors':{'after':'cursor'}}} if len(calls)==1 else {'data':[2]}
    meta.get=get
    assert list(meta.pages('me/adaccounts',{}))==[1,2]
    assert calls[1]==('me/adaccounts',{'after':'cursor'})


def test_worker_survives_failure_and_runs_next_job(db,config,monkeypatch):
    db.settings_update({'next_run_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    enqueue(db,'spend-collect');enqueue(db,'spend-export-keitaro')
    def broken(*args):raise RemoteError('Proxy unavailable',transport=True)
    monkeypatch.setattr('app.worker.collect',broken)
    monkeypatch.setattr('app.worker.export',lambda *args:('success','Accepted'))
    w=Worker(db,config);w.tick();w.tick()
    jobs=db.rows('SELECT status FROM jobs ORDER BY id')
    assert [r['status'] for r in jobs]==['error','success']


def test_meta_uses_proxy_and_real_ua_cookie(db,config):
    seed_proxy(db);ingest(db,Ingest(**payload()))
    client=db.one('SELECT * FROM api_clients');proxy=db.one('SELECT * FROM proxies')
    m=Meta(db,config,client,proxy)
    try:
        assert m.session.trust_env is False
        assert m.session.proxies['https']=='http://proxy.example:8080'
        assert m.session.headers['User-Agent']=='Mozilla/5.0 test-UA'
        assert m.session.cookies.get('xs',domain='.facebook.com')=='secret-cookie'
    finally:m.close()


def test_keitaro_accepts_http_ip(config):
    config.keitaro_url='http://45.132.107.144';config.keitaro_key='secret'
    api=Keitaro(config)
    try:
        assert api.base=='http://45.132.107.144/admin_api/v1'
    finally:
        api.close()


def test_keitaro_full_amount_filter_and_unconfirmed_response(config,monkeypatch):
    config.keitaro_url='https://tracker.example';config.keitaro_key='secret'
    calls=[]
    def fake_request(session,method,url,**kwargs):
        calls.append((method,url,kwargs));return {'success':True}
    monkeypatch.setattr('app.integrations.request_json',fake_request)
    api=Keitaro(config)
    row={'keitaro_campaign_id':12,'meta_campaign_id':'456','day':'2026-09-05','spend_micros':11220000,'currency':'USD','timezone':'Europe/Kyiv'}
    try:
        api.update_costs(row)
        method,url,kwargs=calls[0]
        assert method=='POST' and url.endswith('/campaigns/12/update_costs')
        assert kwargs['json']['cost']=='11.220000'
        assert kwargs['json']['filters']=={'sub_id_2':'456'}
        assert kwargs['json']['end_date']=='2026-09-05 23:59:59'
        monkeypatch.setattr('app.integrations.request_json',lambda *a,**k:{'success':False})
        with pytest.raises(RemoteError):api.update_costs(row)
    finally:api.close()


def test_report_pagination_and_mapping_dedup(config,monkeypatch):
    config.keitaro_url='https://tracker.example';config.keitaro_key='secret'
    calls=[]
    def fake_request(session,method,url,**kwargs):
        calls.append(kwargs['json'])
        if len(calls)==1:return {'rows':[{'campaign_id':i+1,'sub_id_2':'456'} for i in range(1000)]}
        return {'rows':[{'campaign_id':1001,'sub_id_2':'456'}]}
    monkeypatch.setattr('app.integrations.request_json',fake_request)
    api=Keitaro(config)
    try:
        result=list(api.mappings(date(2026,9,1),date(2026,9,5)))
        assert len(result)==1001 and calls[1]['offset']==1000
        assert calls[0]['dimensions']==['campaign_id','sub_id_2']
    finally:api.close()
