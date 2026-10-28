import os
import sys

import pytest
import requests
from google.cloud import firestore

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _通信を禁止する(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("テストから外部へ通信できません")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(firestore, "Client", blocked)
