from __future__ import annotations

from typing import Any

import dns.asyncresolver
import dns.exception
import dns.resolver


class DomainVerificationUnavailable(RuntimeError):
    pass


def _txt_values(answer: Any) -> list[str]:
    values: list[str] = []
    for record in answer:
        strings = getattr(record, "strings", None)
        if strings is not None:
            values.append(b"".join(strings).decode("utf-8", "replace"))
        else:
            values.append(record.to_text().replace('" "', "").strip('"'))
    return values


async def _txt_state(resolver: Any, name: str, prefix: str, *, absent: str) -> str:
    try:
        answer = await resolver.resolve(name, "TXT", lifetime=3.0)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return absent
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        raise DomainVerificationUnavailable("dns_resolver_unavailable") from exc
    return "valid" if any(value.upper().startswith(prefix.upper()) for value in _txt_values(answer)) else "invalid"


async def verify_domain_dns(
    domain: str, metadata: dict[str, Any], *, resolver: Any | None = None,
) -> dict[str, str]:
    active_resolver = resolver or dns.asyncresolver.Resolver()
    selector = metadata.get("dkimSelector")
    if selector is not None and (
        not isinstance(selector, str)
        or not selector
        or len(selector) > 63
        or not selector.replace("-", "").isalnum()
    ):
        raise ValueError("dkim_selector_invalid")
    spf = await _txt_state(active_resolver, domain, "v=spf1", absent="invalid")
    dmarc = await _txt_state(active_resolver, f"_dmarc.{domain}", "v=dmarc1", absent="invalid")
    dkim = (
        await _txt_state(active_resolver, f"{selector}._domainkey.{domain}", "v=dkim1", absent="invalid")
        if selector else "not_configured"
    )
    bimi = await _txt_state(active_resolver, f"default._bimi.{domain}", "v=bimi1", absent="not_configured")
    return {
        "spf": spf, "dkim": dkim, "dmarc": dmarc, "bimi": bimi,
        # These require the selected delivery provider/IP and are deliberately
        # not inferred from public DNS before a provider route is approved.
        "reverse_dns": "not_configured", "tls": "not_configured",
    }
