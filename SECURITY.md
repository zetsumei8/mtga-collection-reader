# Security policy

Please report suspected vulnerabilities privately to the maintainer rather than opening a public issue. Include reproduction steps, affected version, and impact. Do not include collection exports, database files, access tokens, or personal data in a report.

Security boundaries: the application is Windows-only for automatic sync, binds its optional API only to localhost, requires a bearer token, and uses read-only access through an independently distributed helper. Reports involving path traversal, token disclosure, unsafe helper installation, local API authorization, or unintended writes are especially welcome.
