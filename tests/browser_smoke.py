"""Optional browser check: pip install playwright, then python -m tests.browser_smoke.

Uses installed Chrome (no browser download) and a temporary DB, never live credentials.
"""
import socket
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet
import uvicorn
from playwright.sync_api import sync_playwright
from app.config import Config
from app.main import create_app
from app.credentials import ingest
from app.models import Ingest, ProxyInput
from app.integrations import save_proxy
from app.spend import persist_insights
from tests.test_core import payload, insight, account


def main():
    output = Path('data/browser-check')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as temp:
        config = Config(str(Path(temp)/'test.sqlite'), 'browser-test-password-'+'x'*30,
                        'client-test-key', Fernet.generate_key().decode(), scheduler=False, secure_cookie=False)
        app = create_app(config)
        db = app.state.db
        ingest(db, Ingest(**payload()))
        pid = save_proxy(db, ProxyInput(name='Demo proxy',host='proxy.example',port=8080))
        db.execute("UPDATE proxies SET is_primary=1,status='active',ip='203.0.113.10' WHERE id=?",(pid,))
        a = account(db,tz='UTC')
        today = datetime.now(timezone.utc).date()
        for offset in range(14):
            day = today-timedelta(days=offset)
            persist_insights(db,a,[insight(str(day),str(120+offset*17))],day,day,1,datetime.now(timezone.utc))
        sock = socket.socket();sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='chrome',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1100})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{port}')
                page.locator('#password').fill(config.password)
                page.locator('#login-form button').click()
                page.locator('#shell').wait_for(state='visible')
                page.locator('#chart .bar').first.wait_for()
                page.screenshot(path=str(output/'desktop.png'),full_page=True)
                assert page.locator('#today-total').inner_text()!='0.00'
                page.locator('nav [data-tab="clients"]').click()
                page.locator('#client-table').get_by_text('Mozilla/5.0 test-UA').wait_for()
                assert 'secret-cookie' not in page.locator('#client-table').inner_text()
                page.locator('nav [data-tab="proxies"]').click()
                page.locator('#new-proxy').click()
                page.locator('#proxy-raw').fill('http://new.example:9090:user:pass[https://refresh.example/change]')
                page.locator('#parse-proxy').click()
                page.wait_for_function("document.querySelector('#proxy-form [name=host]').value === 'new.example'")
                page.locator('#proxy-form [type=submit]').click()
                page.wait_for_function("document.querySelectorAll('.proxy-card').length === 2")
                page.locator('nav [data-tab="settings"]').click()
                page.locator('[name=commission_percent]').fill('12.5')
                page.locator('#settings-form [type=submit]').click()
                page.wait_for_function("document.querySelector('#notice').textContent.includes('збережено')")
                assert db.settings()['commission_percent']=='12.5'
                for tab in ['exports','logs','dashboard']:
                    page.locator(f'nav [data-tab="{tab}"]').click()
                    page.locator('#'+tab).wait_for(state='visible')
                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(output/'mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.locator('#run-now').click()
                page.wait_for_function("document.querySelector('#notice').textContent.includes('чергу')")
                assert len(db.rows('SELECT * FROM jobs WHERE force_export=1'))==2
                assert not errors,errors
                print('Browser OK: login, dashboard, clients, proxy parser/save, settings, logs/exports, mobile, force update. No JS errors.')
                browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=5);sock.close()


if __name__=='__main__':
    main()
