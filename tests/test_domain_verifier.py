from __future__ import annotations

import dns.exception
import dns.resolver
import pytest

from app.domain_verifier import DomainVerificationUnavailable, verify_domain_dns


class Txt:
    def __init__(self, value: str):
        self.strings = [value.encode()]


class Resolver:
    def __init__(self, values: dict[str, str], timeout: bool = False):
        self.values = values
        self.timeout = timeout

    async def resolve(self, name: str, _kind: str, *, lifetime: float):
        assert lifetime == 3.0
        if self.timeout:
            raise dns.exception.Timeout
        if name not in self.values:
            raise dns.resolver.NoAnswer
        return [Txt(self.values[name])]


@pytest.mark.asyncio
async def test_dns_verification_requires_real_spf_dkim_and_dmarc_evidence():
    result = await verify_domain_dns(
        "example.invalid", {"dkimSelector": "codestra"},
        resolver=Resolver({
            "example.invalid": "v=spf1 -all",
            "_dmarc.example.invalid": "v=DMARC1; p=reject",
            "codestra._domainkey.example.invalid": "v=DKIM1; p=synthetic-public-key",
        }),
    )
    assert result["spf"] == result["dkim"] == result["dmarc"] == "valid"
    assert result["bimi"] == "not_configured"
    assert result["reverse_dns"] == result["tls"] == "not_configured"


@pytest.mark.asyncio
async def test_dns_verification_does_not_invent_missing_or_unavailable_evidence():
    missing = await verify_domain_dns("example.invalid", {}, resolver=Resolver({}))
    assert missing["spf"] == missing["dmarc"] == "invalid"
    assert missing["dkim"] == "not_configured"
    with pytest.raises(DomainVerificationUnavailable):
        await verify_domain_dns("example.invalid", {}, resolver=Resolver({}, timeout=True))


@pytest.mark.asyncio
async def test_dns_verification_rejects_unbounded_selector():
    with pytest.raises(ValueError, match="dkim_selector_invalid"):
        await verify_domain_dns("example.invalid", {"dkimSelector": "bad.selector"}, resolver=Resolver({}))
