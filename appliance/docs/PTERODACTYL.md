# Pterodactyl CPU and optional CUDA operation

CPU mode is the default. Build `Dockerfile.cpu`, set `LOCAL_AI_DEVICE=cpu` and `LOCAL_AI_ONLY=true`; no NVIDIA runtime or GPU allocation is needed. Start with one simultaneous call and the CPU budgets in [README.md](../README.md). Available RAM gives model-loading headroom, but the CPU quota, affinity, and competing workloads still determine response time. Use the new CPU image digest; the historical `gpu-appliance` workflow builds the separate unified image.

For optional CUDA mode, explicitly set `LOCAL_AI_DEVICE=cuda`. The node must expose an NVIDIA runtime to Docker before Wings starts this profile. Verify on the host and inside a test container:

```bash
nvidia-smi
```

The CUDA preflight requires at least 7000 MiB visible. CPU preflight skips NVIDIA checks. Both profiles still require model files, private inference/audio services, and valid carrier configuration.

The earlier CUDA deployment used these resources:

- RTX A1000 8 GB
- 8 CPU cores minimum, 12 preferred
- 32 GB RAM minimum, 64 preferred
- 150 GB free NVMe for image, models, logs, recordings, and backups
- Stable public IP and UPS

Allocations:

- `8002/tcp` web panel
- `5060/udp` or carrier-selected SIP port
- `10000-10100/udp` RTP

Do not expose ports 8081 or 8090. They are loopback-only LLM and AudioSocket services.

The first model download can take several minutes. Pterodactyl should not route production calls until `/ready` returns 200.
