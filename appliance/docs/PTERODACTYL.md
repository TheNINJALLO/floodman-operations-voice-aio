# Pterodactyl and RTX A1000

The node must expose an NVIDIA runtime to Docker before Wings starts this egg. Verify on the host and inside a test container:

```bash
nvidia-smi
```

The A1000 should report roughly 8 GB VRAM. The container preflight requires at least 7000 MiB visible.

Recommended host resources:

- RTX A1000 8 GB
- 8 CPU cores minimum, 12 preferred
- 32 GB RAM minimum, 64 preferred
- 150 GB free NVMe for image, models, logs, recordings, and backups
- Stable public IP and UPS

Allocations:

- `8003/tcp` web panel
- `5060/udp` or carrier-selected SIP port
- `10000-10100/udp` RTP

Do not expose ports 8081 or 8090. They are loopback-only LLM and AudioSocket services.

The first model download can take several minutes. Pterodactyl should not route production calls until `/ready` returns 200.
