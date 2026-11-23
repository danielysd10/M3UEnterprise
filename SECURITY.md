# Security policy

## Supported release

Only the latest verified `M3U_ENTERPRISE_PRODUCTION_READY_OPTIMIZED_V*.zip`
release is supported. Before use, verify the ZIP and its external SBOM with a
trusted verifier for that release. When available, also verify the GitHub
Actions artifact attestation. [Release verification guide](docs/RELEASE_PACKAGING.md).

## Reporting a vulnerability

Report security issues affecting `danielysd10/M3UEnterprise` privately by email
to `danielyesid.dg.35@gmail.com`. Include the affected version and a description
of the issue. Redact stream URLs, tokens, provider credentials, private IPs
and XMLTV sources before sending. Keep vulnerability details out of public issues.

## Local private data

Keep operational playlists and their inventories in private storage. The source
ZIP excludes them. Playlists can contain provider endpoints, credentials or
expiring tokens. Keep their contents out of tickets, logs, commits, CI artifacts
and chat messages.

## Production boundary

Automated tests do not authorize external traffic. Production network access
requires the approved outbound proxy, `M3U_EGRESS_GUARD=1`, an authorized data
set and a dated validation record. Keep `M3U_ALLOW_PRIVATE_NETWORKS` disabled in
production.
