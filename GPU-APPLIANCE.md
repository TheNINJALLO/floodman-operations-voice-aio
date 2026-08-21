# Floodman GPU Appliance

The clean self-hosted voice system lives in [`appliance/`](appliance/).

It is built separately from the legacy AIO image and published as:

```text
ghcr.io/theninjallo/floodman-operations-voice-aio:gpu-appliance
```

This does not replace the current `latest` image. See
[`appliance/CURRENT-AIO-INTEGRATION.md`](appliance/CURRENT-AIO-INTEGRATION.md)
for the staged migration and acceptance process.


## Workflow permissions

The generated branch intentionally contains no new root
`.github/workflows/*.yml` file. GitHub rejects those changes when they are
pushed by a GitHub App token without the separate Workflows permission. The
installer publishes the first image directly and retains the reusable
template at `appliance/ci/ci-gpu-appliance.yml`.
