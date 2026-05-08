"""Regression test: wiki_add must survive content with emoji, em dashes, and [[wikilinks]].

Root cause (v4.3 fix): storage._q() used json.dumps with ensure_ascii=True, encoding emoji
as \\uD83D\\uDEA8 surrogate pairs that SurrealDB v3 rejects with HTTP 400. The fix switches
to ensure_ascii=False so all non-ASCII passes as raw UTF-8.

This test exercises the full storage round-trip so it will catch any future regression in
how special characters are serialised into SurrealQL parameters.

NOTE: only catches the v4.3 regression in HTTP/server mode (where _q() builds the LET
statement). Embedded mode bypasses that path and would round-trip the content even without
the fix. The conftest fixture starts a real SurrealDB process if `surreal` is on PATH.
"""

import pytest

from yadgar import server

ROUTE53_FIXTURE = """\
# AWS Route53 glossary — account 488021763009

**Slug patterns:**
- Hosted zones: `zone-<name-with-dashes>` (e.g., `zone-quinyx-com`)
- Registered domains: `domain-<name-with-dashes>`

**Index:** [[aws-inventory-index-account-488021763009]]

## Hosted zones — by category

### Application apex zones
- [[zone-quinyx-com]] — **1,475 records** (driven by Kubernetes external-dns)
- [[zone-quinyx-io]] — **384 records**

## Renewal red flags

**🚨 ExpirationDate already past as of 2026-05-07** (auto-renew shows "on" but date hasn't moved):
- [[domain-quinyx-mx]] — 2026-01-31
- [[domain-quinyx-fi]] — 2026-02-06

## Operational quirk
- `aws route53domains` API requires `--region us-east-1` regardless of CLI default ↔ hardcode in TF.
"""

VPC_FIXTURE = """\
# AWS VPC inventory index — account 488021763009

| VPC | Region | CIDR | Purpose |
|---|---|---|---|
| vpc-abc123 | eu-west-1 | 10.0.0.0/16 | Production ↔ main |

🚨 Note: overlapping CIDR ranges detected — review before peering.

Em dash test: foo — bar
"""


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "escape_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


class TestWikiSQLEscape:
    def test_route53_content_roundtrips(self):
        """Content with emoji, em dashes, and [[wikilinks]] must survive add → read."""
        wiki = server._wiki
        assert wiki is not None

        result = wiki.add(
            title="AWS Route53 glossary",
            content=ROUTE53_FIXTURE,
            category="reference",
            tags=["aws", "route53"],
        )
        assert result.get("slug") == "aws-route53-glossary"

        page = wiki.read("aws-route53-glossary")
        assert page is not None
        assert "🚨" in page["content"]
        assert "—" in page["content"]
        assert "[[zone-quinyx-com]]" in page["content"]

    def test_vpc_content_with_arrows_and_emoji(self):
        """Content with ↔ and 🚨 must survive the storage round-trip."""
        wiki = server._wiki
        assert wiki is not None

        wiki.add(title="AWS VPC inventory", content=VPC_FIXTURE, category="reference")
        page = wiki.read("aws-vpc-inventory")
        assert page is not None
        assert "↔" in page["content"]
        assert "🚨" in page["content"]
        assert "—" in page["content"]

    def test_update_preserves_special_chars(self):
        """Updating a wiki page with emoji content must not corrupt existing data."""
        wiki = server._wiki
        assert wiki is not None

        wiki.add(title="Emoji page", content="First version — no emoji")
        wiki.add(title="Emoji page", content="Updated — now has 🚨 emoji ↔ test")

        page = wiki.read("emoji-page")
        assert page is not None
        assert "🚨" in page["content"]
        assert "↔" in page["content"]
