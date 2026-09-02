from app.main import EXTERNAL_DELIVERY_ENABLED, app, capabilities


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
