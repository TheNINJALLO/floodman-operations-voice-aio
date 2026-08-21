# GitHub workflow-file permissions

The installer runs with the repository `GITHUB_TOKEN`, which is a GitHub App
installation token. `contents: write` allows ordinary source changes, but
GitHub applies a separate Workflows permission to root
`.github/workflows/*.yml` files.

The appliance integration therefore works this way:

- The installer tests and publishes the first GPU image directly.
- The generated branch contains no new root workflow file.
- The reusable workflow template lives at
  `appliance/ci/ci-gpu-appliance.yml`.
- Copy the template to `.github/workflows/ci-gpu-appliance.yml` manually only
  with a user or app credential allowed to manage workflow files.
