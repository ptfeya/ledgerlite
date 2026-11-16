import logging
from contextlib import contextmanager

import pytest
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout

from tests.helpers import TEST_PASSWORD, csrf_from_html


@pytest.mark.parametrize("path", ["/auth/login", "/ui/login"])
@pytest.mark.parametrize("error_type", [OperationalError, InterfaceError, PoolTimeout])
@pytest.mark.parametrize("stage", ["acquire", "query"])
def test_login_backend_failure_is_safe_and_retryable(
    client, monkeypatch, caplog, path, error_type, stage
):
    csrf = csrf_from_html(client.get("/ui/login").text)
    credentials = {"email": "alice@example.com", "password": TEST_PASSWORD}
    private_values = (*credentials.values(), "private-token", csrf)
    failure_message = " ".join(private_values)

    class UnavailableConnection:
        def execute(self, *args, **kwargs):
            raise error_type(failure_message)

    @contextmanager
    def unavailable_connection():
        if stage == "acquire":
            raise error_type(failure_message)
        yield UnavailableConnection()

    if path == "/auth/login":
        request = {"json": credentials}
    else:
        request = {"data": {**credentials, "csrf_token": csrf}}
    request["headers"] = {"Authorization": "Bearer private-token"}

    with monkeypatch.context() as patch:
        patch.setattr(client.app.state.pool, "connection", unavailable_connection)
        with caplog.at_level(logging.ERROR, logger="app.auth"):
            response = client.post(path, follow_redirects=False, **request)

    assert response.status_code == 503
    assert client.cookies.get("ll_session") is None
    if path == "/auth/login":
        assert response.json() == {"detail": "Service temporarily unavailable"}
    else:
        assert response.headers["content-type"].startswith("text/html")
        assert '<p class="error">Service temporarily unavailable</p>' in response.text
        assert csrf_from_html(response.text) == csrf

    records = [record for record in caplog.records if record.name == "app.auth"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == (
        f"Authentication database unavailable ({error_type.__name__})"
    )
    assert records[0].exc_info is None
    for value in private_values:
        assert value not in caplog.text

    retry = client.post(path, follow_redirects=False, **request)
    assert retry.status_code == (200 if path == "/auth/login" else 303)


@pytest.mark.parametrize("path", ["/auth/login", "/ui/login"])
@pytest.mark.parametrize("field", ["email", "password"])
@pytest.mark.parametrize("value", [None, ""])
def test_login_missing_or_empty_fields(client, path, field, value):
    credentials = {"email": "alice@example.com", "password": TEST_PASSWORD}
    if value is None:
        credentials.pop(field)
    else:
        credentials[field] = value

    if path == "/auth/login":
        response = client.post(path, json=credentials)
        assert response.headers["content-type"].startswith("application/json")
        assert "access_token" not in response.json()
    else:
        csrf = csrf_from_html(client.get("/ui/login").text)
        response = client.post(path, data={**credentials, "csrf_token": csrf})
        assert response.headers["content-type"].startswith("text/html")
        assert '<p class="error">Enter your email and password.</p>' in response.text
        assert csrf_from_html(response.text) == csrf

    assert response.status_code == 422
    assert client.cookies.get("ll_session") is None


@pytest.mark.parametrize("path", ["/auth/login", "/ui/login"])
def test_login_credential_errors_are_indistinguishable(client, path):
    csrf = csrf_from_html(client.get("/ui/login").text)
    responses = []
    for credentials in (
        {"email": "unknown@example.com", "password": TEST_PASSWORD},
        {"email": "alice@example.com", "password": "wrong-password"},
    ):
        if path == "/auth/login":
            response = client.post(path, json=credentials)
        else:
            response = client.post(path, data={**credentials, "csrf_token": csrf})
        responses.append(response)

    assert all(response.status_code == 401 for response in responses)
    assert responses[0].text == responses[1].text
    if path == "/auth/login":
        assert responses[0].json() == {"detail": "Invalid credentials"}
    else:
        assert '<p class="error">Invalid credentials</p>' in responses[0].text
    assert client.cookies.get("ll_session") is None


def test_missing_login_fields_do_not_bypass_csrf(client):
    response = client.post("/ui/login", data={})
    assert response.status_code == 403
    assert client.cookies.get("ll_session") is None
