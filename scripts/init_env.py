"""Create secrets locally, without printing them or overwriting an existing .env."""
import base64
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parent.parent
target = root / '.env'
if target.exists():
    print('.env already exists; kept unchanged.')
else:
    content = (root / '.env.example').read_text(encoding='utf-8')
    content = content.replace('GENERATE_APP_PASSWORD', secrets.token_urlsafe(36))
    content = content.replace('GENERATE_CLIENT_KEY', secrets.token_urlsafe(36))
    content = content.replace('GENERATE_ENCRYPTION_KEY', base64.urlsafe_b64encode(os.urandom(32)).decode())
    with target.open('x', encoding='utf-8') as file:
        file.write(content)
    target.chmod(0o600)
    print('Created .env with generated password and keys. Open the file to configure Keitaro.')
