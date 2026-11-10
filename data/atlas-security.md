# Atlas Platform — Security and Access

Atlas encrypts data in transit with TLS 1.3 and at rest with AES-256. Database encryption keys are managed by the platform and rotated automatically every 90 days. Customer supplied encryption keys are available on the Enterprise plan only and are never stored in the same account as the encrypted data.

API keys are scoped to a workspace and expire after 365 days. Keys can be rotated at any time from the developer settings page, and the old key stops working 24 hours after the rotation so that running jobs can be moved over. Personal access tokens expire after 90 days and cannot be renewed automatically; the user must authenticate again.

The audit log records every authentication event, every permission change and every export of customer data. Audit events are retained for 400 days and can be streamed to the customer's own storage through a webhook. Audit log entries are immutable and include the actor, the source IP address and the affected resource.

Single sign-on is available with SAML 2.0 on the Team and Enterprise plans. SCIM provisioning is Enterprise only and synchronises users and groups from the identity provider every 40 minutes. When a user is removed from the identity provider the SCIM sync disables the Atlas account on the next run, and any active session is terminated.

Customer data is stored in the region selected when the workspace was created: eu-central for European customers or us-east for everyone else. Data is never replicated across regions, including for backups. Backups are encrypted with the same AES-256 keys as the primary data and are kept for 35 days.

Access to production systems requires a hardware security key and is granted through short lived sessions of at most 8 hours. Standing production access is not granted to engineers; the on-call engineer receives temporary access for the duration of an incident and the access is revoked automatically when the incident is closed.

A suspected security incident follows the same severity ladder as an availability incident, and a SEV1 is declared immediately when customer data may have been accessed. The security team is paged in parallel with the on-call engineer, and the data protection officer joins the incident channel.
