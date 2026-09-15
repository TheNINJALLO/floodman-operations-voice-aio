# Third-Party Notices

Floodman Operations Voice AIO source code is MIT licensed.

The container downloads and runs AVA from:

```text
https://github.com/hkjarral/AVA-AI-Voice-Agent-for-Asterisk
```

at the commit pinned in the Dockerfile. AVA retains its own copyright and MIT license.

The container applies narrow source patches from `scripts/patch_ava.py` to AVA's generic pre-call and in-call HTTP tools and to AVA's local Piper synthesis path. The HTTP patch JSON-escapes dynamic values inserted into JSON request-body string literals. The Piper patch selects `PiperVoice.synthesize_wav` when the installed Piper API supplies it, while preserving AVA's legacy call path. These patches do not remove AVA copyright notices or alter AVA's license.

The full container installs `piper-tts==1.6.0`, which is distributed under GPL-3.0-or-later by the Open Home Foundation voice project. Piper and its bundled components retain their own copyrights and license. The lite image does not install Piper.

The runtime also uses Asterisk, FastAPI, Uvicorn, HTTPX, Pydantic, PyYAML, faster-whisper, SQLite, Supervisor, FFmpeg, optional Vosk, optional llama-cpp-python, and their transitive dependencies. Each retains its own license.

No third-party model weights are redistributed in this source archive. Model downloads remain subject to the model publisher's license and acceptable-use terms.

## Tel-Agent

Floodman's optional primary voice runtime installs Tel-Agent from
<https://github.com/Dpro-at/Tel-Agent> at commit
`8a5193b2d801714e45ae6981a3506a60d562a99e`.

Tel-Agent is licensed under AGPL-3.0-or-later. Its complete pinned source and license
are retained at `/opt/tel-agent` in the distributed container image. The Floodman
AudioSocket adapter in `integrations/tel_agent/` is marked AGPL-3.0-or-later. No
LiveKit package or Tel-Agent `voice` extra is installed.

Anyone distributing a combined image should review the AGPL obligations that apply to
that distribution.
