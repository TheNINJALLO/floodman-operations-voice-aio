# Asterisk configuration

The AIO image generates a minimal embedded Asterisk configuration at startup from environment variables using `scripts/render_asterisk.py`.

For an existing FreePBX deployment, do not replace FreePBX-managed files. Copy the `[floodman-inbound]` context from the generated `extensions.conf` into `extensions_custom.conf`, create a Custom Destination pointing to `floodman-inbound,s,1`, and route the Floodman inbound DID to that destination.
