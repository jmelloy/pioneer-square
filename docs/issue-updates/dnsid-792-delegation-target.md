# Identity-Digital/dnsid#792 — Issue update record

Issue #792 title and body were updated on 2026-05-12 based on the revised specification
introduced in PR #825 (`docs/product/issue-792-delegation-target.md`).

**New title:** Act as DNS delegation target for a managed zone

**Summary of changes:**
- Reframed from a "zone-import" model to a "DNS delegation target" model.
- dnsid becomes the authoritative DNS provider for the zone via registrar NS delegation.
- Added `POST /zones/{domain}/verify-delegation` endpoint spec with `active`/`pending`/`broken` status.
- Explicit out-of-scope note: zone-file import and AXFR are not part of this issue.

Reference: Identity-Digital/dnsid#825
