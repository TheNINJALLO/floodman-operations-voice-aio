# Floodman Website Knowledge Library

The voice agent uses two complementary information layers:

1. `data/config/floodman.yaml` contains short, structured company facts, policies, service-area entries, and operational safety rules.
2. `data/knowledge/` contains approved Markdown used for detailed customer questions.

The search is local, deterministic, and CPU-light. It does not browse the public internet during calls and does not learn from caller statements.

## Persistent layout

```text
data/knowledge/
├── managed/   # installed from the reviewed Floodman website pack
└── custom/    # operator-approved additions, preserved by managed-pack updates
```

A managed update may replace `managed/`. It never deletes `custom/`.

## Document approval

Only Markdown files with `approved: true` in YAML front matter are returned to AVA:

```yaml
---
title: Floodman Warranty Policy
category: policies
approved: true
tags: [warranty, agreement]
source_url: internal-approved-policy
reviewed_at: 2026-08-12
summary: Approved customer-facing warranty explanation.
---
```

Use `approved: false` for drafts, operator notes, or information awaiting review.

## Source policy

The August 12, 2026 managed pack uses the public homepage for company promises and major service descriptions. Blog material is used only as educational context. Testimonials are not treated as standard methods, timelines, or outcomes.

The pack deliberately does not invent office hours, financing, payment policies, warranty terms, insurance direct billing, public address/email information, exact prices, exact response times, or a fire-cleanup service commitment. Add those only after Floodman approves a custom document.

## Runtime behavior

The knowledge base automatically reloads when Markdown files change. Restart the server after editing `data/config/floodman.yaml` or the AVA tool overlay. A Markdown-only custom addition should become searchable without a container rebuild, although restarting after a batch of edits is still a clean operational practice.

## Updating the managed pack

The startup migration is versioned. It backs up the existing Floodman config, AVA overlay, and managed knowledge before installing a new pack. It preserves scheduling, compliance, transfer, Roomflow, upload, and other operational sections from the existing persistent config.

Backups are stored under:

```text
data/backups/knowledge-pack-<version>-<timestamp>/
```

## Test questions

After restart, test at least:

- What services does Floodman offer?
- What happens after a burst pipe?
- Does Floodman use moisture meters or thermal imaging?
- What are signs of foundation trouble?
- Why would someone encapsulate a crawl space?
- Can you identify mold from my description?
- Is the inspection free?
- Can you guarantee arrival within an hour?
- What is my warranty?
- Do you service Grand Rapids?
- Do you service Detroit?

The expected behavior is a supported public answer, a service-area tool result, or an honest human-callback path. The assistant should never manufacture a missing policy.
