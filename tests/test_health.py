def test_health_without_key_or_database(client, tmp_path):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not (tmp_path / "adrobot.db").exists()


def test_home_and_local_css(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>AdRobot</h1>" in response.text
    assert "Создатель кампаний" in response.text
    assert "Редактор офферов" in response.text
    assert "/static/styles.css" in response.text

    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")


def test_openapi_is_available_without_a_cdn(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/openapi.json" in response.text
    assert "/health" in response.text
    assert "<script" not in response.text

    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "get" in schema.json()["paths"]["/health"]
