import pytest
from cryptography.fernet import Fernet
from app.config import Config
from app.db import DB


@pytest.fixture
def config(tmp_path):
    return Config(str(tmp_path/'test.sqlite'), 'test-password-'+'x'*32, 'client-secret', Fernet.generate_key().decode(), secure_cookie=False, scheduler=False)


@pytest.fixture
def db(config):
    return DB(config.database, config.encryption_key)
