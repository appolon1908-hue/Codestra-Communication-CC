from app.main import EXTERNAL_DELIVERY_ENABLED


def test_external_delivery_defaults_off():
    assert EXTERNAL_DELIVERY_ENABLED is False
