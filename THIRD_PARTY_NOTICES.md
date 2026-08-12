# Third-Party Notices

Floodman Operations Voice AIO is MIT licensed.

The container downloads and runs AVA from:

```text
https://github.com/hkjarral/AVA-AI-Voice-Agent-for-Asterisk
```

at the commit pinned in the Dockerfile. AVA retains its own copyright and MIT license.

The container applies a narrow source patch from `scripts/patch_ava.py` to AVA’s generic pre-call and in-call HTTP tools. The patch JSON-escapes dynamic values inserted into JSON request-body string literals. It does not remove AVA copyright notices, alter AVA licensing, or redistribute a modified AVA archive separately from this build process.

The runtime also uses Asterisk, FastAPI, Uvicorn, HTTPX, Pydantic, PyYAML, faster-whisper, SQLite, Supervisor, FFmpeg, optional Piper, optional Vosk, optional llama-cpp-python, and their transitive dependencies. Each retains its own license.

No third-party model weights are redistributed in this source archive. Model downloads remain subject to the model publisher’s license and acceptable-use terms.
