from __future__ import annotations

from fastapi.testclient import TestClient


def test_customer_workspace_keeps_properties_customer_scoped(settings) -> None:
    from app.main import create_app

    headers = {"Authorization": "Bearer admin-test-token"}
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/api/v1/crm/customers",
            headers=headers,
            json={"name": "First Customer", "phone": "+12315551001"},
        ).json()
        second = client.post(
            "/api/v1/crm/customers",
            headers=headers,
            json={"name": "Second Customer", "phone": "+12315551002"},
        ).json()
        first_property = client.post(
            "/api/v1/crm/properties",
            headers=headers,
            json={
                "customer_id": first["id"],
                "address": "101 First Street",
                "city": "Traverse City",
                "state": "MI",
                "zip": "49684",
            },
        ).json()
        client.post(
            "/api/v1/crm/properties",
            headers=headers,
            json={
                "customer_id": second["id"],
                "address": "202 Second Street",
                "city": "Traverse City",
                "state": "MI",
                "zip": "49686",
            },
        )

        bundle = client.get(
            f"/api/v1/crm/customers/{first['id']}", headers=headers
        ).json()
        scoped = client.get(
            "/api/v1/crm/properties",
            headers=headers,
            params={"customer_id": first["id"]},
        ).json()

        assert bundle["id"] == first["id"]
        assert [item["id"] for item in bundle["properties"]] == [first_property["id"]]
        assert {item["customer_id"] for item in scoped} == {first["id"]}
        assert all("Second Street" not in item["address"] for item in scoped)


def test_business_suite_web_app_uses_customer_first_workspace(project_root) -> None:
    html = (project_root / "web/index.html").read_text(encoding="utf-8")
    javascript = (project_root / "web/app.js").read_text(encoding="utf-8")
    stylesheet = project_root / "web/business-suite.css"

    assert "Floodman Business Suite" in html
    assert 'id="customerPickerPanel"' in html
    assert 'id="customerWorkspace"' in html
    assert 'id="propertiesTable"' in html
    assert 'id="propertyForm"' in html
    assert "Only properties belonging to this customer are shown." in html
    assert "/api/v1/crm/customers/${encodeURIComponent(customerId)}" in javascript
    assert "selected.properties||[]" in javascript
    assert stylesheet.is_file()
    assert "/assets/business-suite.css" in html
