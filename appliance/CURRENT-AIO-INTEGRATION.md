# Unified Voice AIO and Business Suite

The production `gpu-appliance` tag is now built from `unified/Dockerfile`. One
Pterodactyl server runs the CUDA voice appliance and the complete Floodman
Business Suite as one supervised runtime.

## Public product surfaces

- `https://aicall.oninetwork.com` -> Voice AIO on port 8002
- `https://floodman.oninetwork.com` -> Business Suite on port 9000
- `https://sign.oninetwork.com` -> signing on port 9001
- port 9002 -> private Mailpit test inbox; do not publish it
- `https://lab.oninetwork.com` -> engineering tools on port 9003
- `https://api.oninetwork.com` -> Business Suite API on port 9004
- SIP 5060 and RTP 10000-10100 remain carrier-facing allocations

ARI, AMI, AudioSocket, local model APIs, PostgreSQL, and SQLite remain loopback
only. Voice AudioSocket uses 8091 because Business Suite competitor intelligence
uses internal port 8090.

## Shared call workflow

Voice records the call and structured intake in local SQLite first. It then
queues signed, ordered call events in a durable local outbox. The background
worker sends them only over loopback to the Business Suite API. Business Suite
creates or updates the linked call card, customer/property draft, RoomFlow job,
unpublished estimate, follow-up task, and eligible staff notifications before
attempting remote ERP synchronization. Business Suite outages never block or
hang up a live call.

Raw transcripts are not copied into the event contract. The Business Suite gets
a secure authenticated Voice AIO transcript link and a concise structured
summary. Consent, suppression, billing, verification, and pricing decisions stay
in deterministic application code.

## Persistent storage

Existing Voice AIO data remains under `/home/container/data`. Business Suite
state is isolated under `/home/container/data/business`. Compatibility paths
such as `/home/container/config` and `/home/container/runtime` are image-owned
symlinks into that persistent directory.

The first unified start creates a persistent Business Suite owner credential
file at `/home/container/data/business/config/unified-owner.env` when owner
values were not supplied. Change that temporary owner password after first
login. SMTP, live SMS, Square, signing delivery, and native mobile push still
require their respective reviewed credentials and approvals.

## Release safety

Every build uses immutable Business Suite, Node, and CUDA image digests. The
release workflow publishes an immutable candidate first. Only a promotion tag
moves `gpu-appliance`, which keeps the previous digest available for rollback.
Acceptance requires Voice `/ready`, Business Hub `/health/live`, Business API
`/health/ready`, a real inbound call, durable call projection, and restart
persistence.
