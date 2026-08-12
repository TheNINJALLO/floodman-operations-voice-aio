# Pterodactyl Import Hotfix v1.1.1

This hotfix corrects the Pterodactyl egg import failure from v1.1.0.

Changes:

- Replaced the non-email egg author with `operations@floodman.com` because Pterodactyl validates egg authors as email addresses.
- Changed boolean variable defaults from `true` / `false` strings to `1` / `0` for Laravel/Pterodactyl compatibility during server creation.
- Replaced the generic Debian installer image with Pterodactyl's maintained Debian installer image.
- Removed the duplicate Docker image choice.
- Set `features` to an explicit empty array.

Import `egg-floodman-operations-voice-aio-v1.1.1.json` from the package root or the matching file under `pterodactyl/`.

## Container image requirement

The egg imports independently of the container image. Before creating or starting the server, publish this source repository to GitHub and allow the included `ci-container.yml` workflow to publish the public `latest` and `lite` GHCR tags. A private or missing GHCR image will cause a pull failure after the egg imports.
