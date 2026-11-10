# Atlas Platform — Incident Response

Incident severity is assigned by the incident commander within 10 minutes of the page. SEV1 means customers cannot use the product at all or data is at risk. SEV2 means a major feature is degraded for a large share of customers. SEV3 is a partial or workaround-available degradation. SEV4 is cosmetic or internal only. The severity drives who gets paged and what the response time is.

A SEV1 pages the on-call engineer, the incident commander and the service owner at the same time, and the acknowledgement is expected within 5 minutes. A SEV2 pages the on-call engineer with a 15 minute acknowledgement target. SEV3 and SEV4 are handled during business hours through the normal ticket queue and never page anyone.

The first decision in any production incident is rollback or forward fix. If the incident started within 30 minutes of a deployment the default answer is rollback, because the rollback window is still open and restoring the previous image takes about 90 seconds. If the window has closed, the incident commander chooses between a feature flag kill switch, a forward fix, or a database level mitigation, and records the reasoning on the incident ticket.

The platform tracks a monthly error budget of 0.1 percent of requests per service. When a service burns more than 2 percent of its monthly budget in one hour, the burn rate alert fires and the on-call engineer must decide whether to freeze changes for that service. Burning the entire budget in a month triggers an automatic change freeze for the following month.

Every SEV1 and SEV2 requires a blameless postmortem. The postmortem document is due within 5 business days of the incident being resolved, and it must contain a timeline, the contributing factors, the customer impact, and action items with an owner and a due date. Postmortems are reviewed in the weekly reliability review meeting.

The incident commander role rotates weekly across senior engineers. The incident commander does not debug; the commander coordinates communication, decides severity, decides rollback or forward fix, and writes the customer facing status updates. This separation keeps the person making decisions from being pulled into the debugging.

Customer communication for a SEV1 starts within 30 minutes on the public status page and is refreshed at least every 60 minutes until the incident is resolved. Legal and support are notified automatically when a SEV1 is declared, and a data protection officer is added to the incident channel when customer data may have been exposed.

Actions from postmortems are tracked in the reliability backlog. An action item that is not completed within 30 days escalates to the engineering director, and any action item that was assigned to prevent a repeat SEV1 is mandatory for the next release planning cycle.
