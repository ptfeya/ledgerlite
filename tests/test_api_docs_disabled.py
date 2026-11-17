import pytest


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_and_schema_are_not_served(client, path):
    assert client.get(path).status_code == 404
