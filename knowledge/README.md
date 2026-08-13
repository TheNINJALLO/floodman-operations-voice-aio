---
title: Knowledge Library Operator Instructions
category: internal
approved: false
tags: [instructions]
source_url: https://floodman.com/
reviewed_at: 2026-08-12
summary: Internal instructions; never returned to callers.
---
# Floodman knowledge library

`managed/` is installed from the reviewed website knowledge pack and may be replaced by a future pack version. `custom/` is for operator-approved additions and is preserved during managed-pack updates.

Every caller-visible Markdown file must start with YAML front matter:

```yaml
---
title: Public title
category: policies
approved: true
tags: [warranty, policy]
source_url: https://example.com/source-or-internal-policy
reviewed_at: 2026-08-12
summary: One-sentence approved summary.
---
```

Only files with `approved: true` are searchable. Write factual, customer-safe language. Do not place secrets, private customer records, card information, passwords, or unapproved legal promises in this library.
