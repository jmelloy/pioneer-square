# Design: Import an Existing DNS Zone as a Managed Zone

**Issue:** [Identity-Digital/dnsid#792](https://github.com/Identity-Digital/dnsid/issues/792)
**Status:** Draft
**Date:** 2026-05-12

---

## 1. Overview & Goals

Users who already operate DNS zones in other systems need a migration path into dnsid rather than recreating every record by hand. This feature adds an **Import Zone** workflow that accepts either a BIND-format zone file (uploaded or pasted) or an AXFR transfer from a live nameserver, parses all records, resolves conflicts, and registers the zone as a fully managed dnsid zone. The imported zone must be indistinguishable from one created from scratch: all records live in the dnsid record store, TTLs and data are preserved verbatim (where supported), and the zone's SOA and apex NS records are replaced with dnsid-managed equivalents. The portal is the primary surface; the API and CLI provide the same capability for automation and power users.

---

## 2. User Journey (Portal)

### Entry Point

The **New Zone** screen gains an **Import existing zone** tab alongside the existing **Create empty zone** tab. The tab is always visible; the AXFR sub-option within it is hidden behind a feature flag (`feature.zone_axfr_import`).

### Step-by-step Flow

| Step | What the user sees | What happens |
|------|--------------------|--------------|
| 1. Select import method | Toggle: **Upload file** / **Paste text** / **AXFR** (if flag enabled) | UI renders the corresponding input widget |
| 2a. Upload file | File picker (`accept=".txt,.zone"`, max 10 MB) | File read client-side; a preview of the first 20 lines appears |
| 2b. Paste text | Textarea (monospace, 20 rows) | Live character count shown |
| 2c. AXFR | Text input for nameserver IP/hostname + optional TSIG key fields | Server-side transfer; loading spinner during fetch |
| 3. Pre-import analysis | "Analyse" button triggers a dry-run call | Returns a summary table: total records, unsupported types (count + list), conflicts (count), SOA/NS records that will be replaced |
| 4. Review conflicts | Conflict table (see §7) with per-record or bulk resolution controls | User picks Skip / Overwrite / Merge for each conflict group |
| 5. Confirm & import | "Import Zone" button; progress bar streams batch updates | Batches of 500 records are committed; progress shows `N / Total records imported` |
| 6. Success state | Green banner: "Zone `example.com` imported — 1 847 records created, 3 skipped, 1 conflict resolved." Link to zone detail page | Zone is now live and managed |
| Error state | Inline error with the failing record's line number and a plain-language explanation | User can correct the file and retry without losing their conflict decisions |

### Progress / Streaming Feedback

For zones under ~500 records the import completes in a single round-trip and shows a brief spinner. For larger zones the portal opens a **Server-Sent Events (SSE)** stream on `GET /zones/{domain}/import/{job_id}/stream`, receiving `progress` events every 250 ms until a `done` or `error` terminal event arrives. A cancel button is available during streaming; partial records committed so far are rolled back on cancellation.

---

## 3. API Contract

### Dry-run Analysis

```
POST /zones/{domain}/import/analyse
Content-Type: multipart/form-data  |  application/json

# multipart: field "zone_file" (file) or "zone_text" (string)
# json for AXFR: { "axfr": { "nameserver": "ns1.example.com", "tsig_key": "..." } }
```

**Response 200:**
```json
{
  "total_records": 1852,
  "unsupported": [{"type": "CAA", "count": 4}, {"type": "SSHFP", "count": 1}],
  "conflicts": [{"name": "mail.example.com", "type": "A", "existing": "1.2.3.4", "incoming": "5.6.7.8"}],
  "soa_replaced": true,
  "apex_ns_replaced": true
}
```

### Import

```
POST /zones/{domain}/import
Content-Type: multipart/form-data  |  application/json

# Same body shape as /analyse, plus:
{
  "conflict_resolution": "skip" | "overwrite" | "merge",
  "per_record_overrides": [
    { "name": "mail.example.com", "type": "A", "resolution": "overwrite" }
  ]
}
```

**Response 202 (async, large zone):**
```json
{ "job_id": "imp_01j...", "stream_url": "/zones/example.com/import/imp_01j.../stream" }
```

**Response 200 (sync, small zone ≤500 records):**
```json
{
  "created": 1847,
  "skipped": 3,
  "conflicts_resolved": 2,
  "errors": []
}
```

### Error Codes

| HTTP | Code | Meaning |
|------|------|---------|
| 400 | `PARSE_ERROR` | Zone file is malformed; `detail` includes line number |
| 400 | `UNSUPPORTED_ORIGIN` | `$ORIGIN` directive conflicts with `{domain}` path param |
| 409 | `ZONE_EXISTS` | Zone already exists and `conflict_resolution` was not provided |
| 422 | `AXFR_REFUSED` | Remote nameserver refused the transfer |
| 413 | `FILE_TOO_LARGE` | Body exceeds 10 MB limit |
| 503 | `AXFR_TIMEOUT` | AXFR did not complete within 30 s |

---

## 4. Zone File Parsing

**Format:** RFC 1035 / RFC 1034 BIND zone-file format.

**Directives supported:**

| Directive | Behaviour |
|-----------|-----------|
| `$ORIGIN` | Sets the base domain; validated against the `{domain}` path param (mismatch → `UNSUPPORTED_ORIGIN`) |
| `$TTL` | Sets the default TTL for subsequent records without explicit TTL |
| `$INCLUDE` | Rejected with a clear error (security boundary — no server-side file inclusion) |
| `$GENERATE` | Expanded into explicit records up to a configurable limit (default 1 000 generated records) |

**Record types fully supported:** A, AAAA, CNAME, MX, TXT, NS, PTR, SRV, NAPTR, CAA, CERT, DNAME, DS, TLSA, HTTPS, SVCB.

**Record types parsed but skipped with a warning:** SSHFP, IPSECKEY, HIP, OPENPGPKEY, SMIMEA — the parser logs them in `unsupported` but does not abort.

**Record types that abort parsing:** ANY, OPT, TKEY, TSIG — these indicate a malformed or non-standard input file.

**Multi-line records** (parenthesised continuation) and **quoted strings** in TXT records are handled per RFC 1035 §5.1. TXT records are stored with their component strings concatenated; strings over 255 bytes are split automatically on write-back.

---

## 5. AXFR Import Path

AXFR import is gated behind the `feature.zone_axfr_import` feature flag, defaulting to **disabled** in all environments until security review is complete.

**Trigger:** The portal sends the nameserver address (IPv4/IPv6 or hostname) and an optional TSIG key (algorithm + base64 secret). The backend initiates the AXFR from the server side, never the client.

**Security controls:**

- The initiating user must have `zone:create` and `zone:import` permissions on the target domain.
- Nameserver addresses are validated against a configurable allowlist (`axfr.allowed_cidr`); by default only public unicast addresses are permitted (RFC 1918 ranges blocked).
- TSIG keys are accepted but never logged or stored after the transfer completes.
- The backend opens a single TCP connection to port 53 of the specified nameserver, with a 30-second overall timeout. DNS rebinding is mitigated by resolving the hostname once and pinning the IP for the duration of the connection.
- Each org is rate-limited to 5 AXFR imports per hour.

**Result:** The AXFR stream is consumed into the same in-memory record list as a parsed zone file, then handed off to the same import pipeline. The calling code path is identical after this point.

---

## 6. SOA & Apex NS Handling

**SOA:** The incoming SOA record is parsed and its `MNAME`, `RNAME`, `SERIAL`, `REFRESH`, `RETRY`, `EXPIRE`, and `MINIMUM` fields are captured for informational purposes (shown in the pre-import analysis summary). The SOA is **not** imported as a user record; dnsid generates and manages its own SOA for every zone. The incoming serial is offered as an optional seed for the dnsid-managed serial to preserve monotonicity, but this is advisory — the platform will increment as needed.

**Apex NS records:** NS records at the zone apex (`@` / bare domain name) are stripped and replaced by dnsid's authoritative nameservers. NS records for delegated subdomains (e.g., `sub.example.com. IN NS ns1.other.`) are imported normally as delegation records.

**Edge cases:**

- If the zone file contains no SOA record, parsing continues (the record is simply absent, not an error), but a warning is surfaced in the analysis UI.
- If apex NS records match dnsid's own nameservers exactly, they are silently dropped (not counted as conflicts).
- Glue records (A/AAAA records for in-zone nameservers of delegated subdomains) are imported as regular records.

---

## 7. Conflict Resolution

A **conflict** occurs when an incoming record's `(name, type)` tuple already exists in the managed zone (relevant for re-imports or zones partially created before the import). For CNAME and records at the zone apex, any collision of name regardless of type is also a conflict.

**Resolution options surfaced to the user:**

| Option | Behaviour |
|--------|-----------|
| **Skip** | The existing record is left untouched; the incoming record is discarded. Counted in `skipped`. |
| **Overwrite** | The existing record is replaced with the incoming record verbatim. |
| **Merge** | For record types that support multiple values (A, AAAA, MX, TXT, NS for subdelegations), the incoming RDATAs are appended to the existing RRset, deduplicating exact matches. For single-value types (CNAME, SOA) merge is not available. |

The portal surfaces conflicts in a paginated table (50 per page). Users can apply a bulk resolution to all conflicts of the same type, then override individual records. Conflict decisions are recorded in the `import_jobs` table so re-runs are reproducible.

---

## 8. Large-Zone Batching

Zones over 500 records are processed asynchronously. The backend splits the validated record list into chunks of 500 and inserts each chunk in a single database transaction. Between chunks, a `progress` SSE event is emitted.

**Partial-failure behaviour:** If a chunk fails (e.g., a database constraint violation on a record that slipped past validation), the failed chunk is rolled back but previously committed chunks are retained. The job transitions to `partial_failure` state; the response lists the failed records with reasons. The user can correct and re-import just the failed records by uploading a corrected file for the same domain — since clean records already exist, unmodified records will resolve as conflicts and can be bulk-skipped.

**Throughput target:** 10 000 records should import in under 60 seconds under normal load.

**Job retention:** Import job records (`import_jobs` table) and their associated conflict decisions are retained for 7 days, then purged.

---

## 9. Acceptance Criteria

**From the issue:**
- [ ] Zone file (BIND-format) can be parsed and imported end-to-end.
- [ ] AXFR import path is available (or stubbed with a clear error if the feature flag is off).
- [ ] Duplicate / conflicting records are reported, not silently dropped.
- [ ] The resulting managed zone is indistinguishable from one created from scratch.
- [ ] Unit tests cover the parser and the import endpoint.

**Portal-specific:**
- [ ] The Import tab is visible on the New Zone screen and toggles between Upload / Paste / AXFR input methods.
- [ ] Dry-run analysis completes before any records are written; the user sees a summary before confirming.
- [ ] Progress bar updates at least every 2 seconds for zones over 500 records.
- [ ] Conflicts are surfaced in a paginated table with per-record and bulk resolution controls.
- [ ] Cancelling a streaming import rolls back all records committed in that job.
- [ ] The success banner shows correct counts for created, skipped, and conflict-resolved records.
- [ ] A parse error displays the offending line number and a plain-language description; no records are written.
- [ ] The AXFR option is hidden when `feature.zone_axfr_import` is disabled.

---

## 10. Open Questions & Risks

1. **DNSSEC records:** The issue is silent on DNSSEC. Importing a signed zone will include RRSIG, DNSKEY, NSEC/NSEC3, and DS records. If dnsid manages signing, importing these records could produce an inconsistent signed zone. Should DNSSEC-related record types (RRSIG, DNSKEY, NSEC, NSEC3, NSEC3PARAM, DS at apex) be stripped automatically, or surfaced as a hard error requiring the user to acknowledge that signing state will be reset?

2. **Ownership verification:** Should we require the user to prove they control the zone before importing (e.g., a TXT record challenge or SOA serial verification)? Without this, any user with `zone:create` permission could import a zone they don't own, which could cause confusion if the true owner later tries to onboard. What is the intended trust model?

3. **Re-import / incremental sync:** The current design treats import as a one-shot migration. If a user imports, makes changes in dnsid, and then wants to re-sync from the upstream zone, the conflict surface grows considerably. Should we support a "reconcile" mode that only applies changes rather than treating every existing record as a potential conflict? This has significant scope implications.

4. **File size and memory:** A 10 000-record zone file with long TXT records could approach the 10 MB limit. Parsing the entire file into memory before analysis means peak memory usage per import worker could be significant under concurrent load. Consider whether streaming parse (line-by-line) is feasible for the MVP or if the memory cap and request limit are sufficient guards for now.

5. **Audit trail:** Import jobs modify potentially thousands of records in a single user action. The existing audit log likely records individual record operations. For a bulk import this creates very noisy audit logs. Should imports be logged as a single audit event with a summary, with the per-record detail stored in the `import_jobs` table rather than the main audit log?
