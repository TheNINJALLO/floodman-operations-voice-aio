# Migration

Keep the current Voice AIO server online while building this appliance.

1. Back up the old `/home/container/data` directory.
2. Deploy the new appliance under a temporary DID or separate Twilio origination route.
3. Copy the old approved `knowledge/` directory only after reviewing custom files.
4. Preserve the old SQLite databases and recordings as read-only audit material.
5. Configure team recipients and perform simulator tests.
6. Perform phone acceptance tests.
7. Move the production DID only after the full test matrix passes.

The appliance intentionally does not import old AVA provider configuration or cloud AI keys.
