from app.main import EXTERNAL_DELIVERY_ENABLED, app, capabilities


def test_canonical_communications_route_is_plural_and_stable():
    paths = set(app.openapi()["paths"])
    assert "/v1/communications/messages" in paths
    assert "/v1/communications/messages/{message_id}" in paths
    assert "/v1/communications/capabilities" in paths
    assert "/v1/communications/providers" in paths
    assert "/v1/messages" not in paths
    assert "/v1/communication/messages" not in paths


def test_policy_capabilities_are_real_but_delivery_stays_disabled():
    value = capabilities()
    assert value["consent_enforcement"] is True
    assert value["suppression_enforcement"] is True
    assert value["external_delivery"] is False
    assert EXTERNAL_DELIVERY_ENABLED is False
