from app.models import Message, Consent, Suppression


def test_message_tenant_boundary():
    assert Message.__table__.columns["tenant_id"].nullable is False


def test_consent_tenant_boundary():
    assert Consent.__table__.columns["tenant_id"].nullable is False


def test_suppression_tenant_boundary():
    assert Suppression.__table__.columns["tenant_id"].nullable is False
