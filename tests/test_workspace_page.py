from html.parser import HTMLParser


class Workspace(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}
        self.resources = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if "id" in fields:
            self.elements[fields["id"]] = (tag, fields)
        for attribute in ("src", "href"):
            if attribute in fields:
                self.resources.append(fields[attribute])


def test_workspace_forms_and_local_assets(client):
    response = client.get("/")
    page = Workspace()
    page.feed(response.text)
    assert response.status_code == 200
    for control, tag in {
        "create-form": "form", "campaign-name": "input", "geo": "input",
        "create-query": "input", "check-operation": "button", "new-operation": "button",
        "campaign": "select", "stream": "select", "editor-query": "input",
        "offer-rows": "tbody", "reload-editor": "button", "refresh-editor": "button",
        "cancel-editor": "button", "push-editor": "button",
    }.items():
        assert page.elements[control][0] == tag
    for feedback in ("create-status", "editor-status", "create-search-status", "draft-state"):
        assert page.elements[feedback][1]["role"] == "status"
    assert set(page.resources) == {"/docs", "/health", "/static/styles.css", "/static/workspace.js"}


def test_workspace_assets_contain_no_server_secret_or_external_resources(database_url):
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    app = create_app(Settings(database_url=database_url, keitaro_api_key="fixture-key-private-marker"))
    with TestClient(app) as client:
        check_assets(client)


def check_assets(client):
    for path in ("/", "/static/styles.css", "/static/workspace.js", "/static/ui.js",
                 "/static/creator.js", "/static/editor.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert "fixture-key-private-marker" not in response.text
        assert "https://" not in response.text and "http://" not in response.text
        assert "innerHTML" not in response.text
