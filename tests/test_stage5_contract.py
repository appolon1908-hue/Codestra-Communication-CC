import pytest

from app.main import EXTERNAL_DELIVERY_ENABLED, TemplateRenderRequest, app, capabilities, provider_health


def test_committed_openapi_matches_runtime():
    import json
    from pathlib import Path

    committed = json.loads(
        (Path(__file__).parents[1] / "contracts/openapi.v1.json").read_text()
    )
    assert committed == app.openapi()


def test_canonical_communications_route_is_plural_and_stable():
    paths = set(app.openapi()["paths"])
    assert "/v1/communications/messages" in paths
    assert "/v1/communications/messages/{message_id}" in paths
    assert "/v1/communications/capabilities" in paths
    assert "/v1/communications/providers" in paths
    assert "/v1/communications/operations" in paths
    assert "/v1/communications/operations/{operation_id}" in paths
    assert "/v1/communications/operations/{operation_id}/reconcile" in paths
    assert "/v1/communications/preferences" in paths
    assert "/v1/communications/recipients/{recipient_id}/preferences" in paths
    assert "/v1/communications/templates/{template_id}/render" in paths
    template_methods = app.openapi()["paths"]["/v1/communications/templates/{template_id}"]
    assert {"get", "put", "patch", "delete"}.issubset(template_methods)
    assert "/v1/communications/provider-health" in paths
    assert "/v1/communications/usage" in paths
    assert "/v1/communications/domains" in paths
    assert "/v1/communications/domains/{domain_id}" in paths
    assert "/v1/communications/domains/{domain_id}/verify" in paths
    assert "/v1/communications/sender-identities" in paths
    assert "/v1/communications/sender-identities/{sender_identity_id}" in paths
    assert {"get", "put", "post"}.issubset(
        app.openapi()["paths"]["/v1/communications/preferences"]
    )
    assert "/v1/messages" not in paths
    assert "/v1/communication/messages" not in paths


def test_policy_capabilities_are_real_but_delivery_stays_disabled():
    value = capabilities()
    assert value["consent_enforcement"] is True
    assert value["suppression_enforcement"] is True
    assert value["external_delivery"] is False
    assert EXTERNAL_DELIVERY_ENABLED is False


def test_template_render_variables_are_bounded():
    with pytest.raises(ValueError):
        TemplateRenderRequest(variables={"bad variable": "x"})
    with pytest.raises(ValueError):
        TemplateRenderRequest(variables={f"v{i}": "x" for i in range(201)})


def test_provider_health_reports_disabled_instead_of_fabricating_probe_success():
    report = provider_health()
    assert report.status == "disabled"
    assert report.providers
    assert {item.status for item in report.providers} == {"disabled"}
