from app.models import ConsentModel, MessageModel, SuppressionModel


def test_all_communication_records_have_required_tenant_boundaries():
    for model in (MessageModel, ConsentModel, SuppressionModel):
        assert model.__table__.columns["tenant_id"].nullable is False
