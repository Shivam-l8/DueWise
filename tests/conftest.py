import re

import pytest

from app.web import create_app
from app.workspace import workspace_db


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / 'instance')
    app.config.update(TESTING=True)
    return app


def csrf(client):
    with client.session_transaction() as session:
        token = session.get('_csrf')
    if token:
        return token
    page = client.get('/welcome')
    return re.search(rb'name="_csrf" value="([^"]+)"', page.data)[1].decode()


def register(client, username='test-user'):
    response = client.post('/register', data={
        'username': username, 'password': 'fictional-password', '_csrf': csrf(client),
    })
    assert response.status_code == 302 and response.location == '/'
    return client


@pytest.fixture
def workspace(app):
    client = register(app.test_client())
    with client.session_transaction() as session:
        user_id = session['user']
    with app.app_context():
        return client, workspace_db(user_id)
