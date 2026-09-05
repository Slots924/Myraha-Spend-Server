from fastapi.testclient import TestClient
from app.main import create_app
from .test_core import payload


def test_login_ingest_validation_auth_and_ui(config):
    app=create_app(config)
    with TestClient(app) as c:
        assert c.get('/').status_code==200
        assert c.get('/api/clients').status_code==401
        assert c.post('/fb_data/add',json=payload()).status_code==401
        assert c.post('/fb_data/add',headers={'X-Client-Key':config.client_key},json=payload()).json()=={'ok':True}
        headers={'X-Requested-With':'Myraha'}
        assert c.post('/api/login',headers=headers,json={'password':config.password}).status_code==200
        r=c.get('/api/clients');assert r.status_code==200
        assert 'secret-cookie' not in r.text and 'EAA-test-token-123456789' not in r.text
        assert c.put('/api/settings',json={}).status_code==403
        assert c.put('/api/settings',headers={**headers,'Origin':'https://evil.example'},json={}).status_code==403
        bad=payload();bad['cookies']['items'][0]['domain']='evil.example'
        r=c.post('/fb_data/add',headers={'X-Client-Key':config.client_key},json=bad)
        assert r.status_code==400 and 'secret-cookie' not in r.text
        r=c.post('/api/run/all',headers=headers,json={});assert r.status_code==200
        assert all(x['force_export']==1 for x in app.state.db.rows('SELECT * FROM jobs'))
        assert c.get('/api/dashboard').status_code==200
        assert c.post('/api/logout',headers=headers,json={}).status_code==200
        assert c.get('/api/clients').status_code==401


def test_rate_limit_and_body_limit(config):
    with TestClient(create_app(config)) as c:
        headers={'X-Requested-With':'Myraha'}
        for _ in range(10):assert c.post('/api/login',headers=headers,json={'password':'wrong'}).status_code==401
        assert c.post('/api/login',headers=headers,json={'password':config.password}).status_code==429
        assert c.post('/fb_data/add',content='x'*1048577,headers={'Content-Type':'application/json'}).status_code==413
