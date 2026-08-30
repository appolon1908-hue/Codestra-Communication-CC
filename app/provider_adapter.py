from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class DeliveryRequest:
    channel: str
    recipient: str
    template_key: str
    rendered_body: str | None = None

@dataclass(frozen=True)
class DeliveryResult:
    provider_message_id: str
    status: str

class CommunicationProvider(Protocol):
    async def send(self, request: DeliveryRequest) -> DeliveryResult: ...

class DisabledProvider:
    async def send(self, request: DeliveryRequest) -> DeliveryResult:
        raise RuntimeError("external_delivery_disabled")
