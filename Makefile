SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PYTHON ?= python3
RUNTIME_ENV ?= .env
TWILIO_PROVISIONING_ENV ?= .env.twilio-provisioning
BACKUP_OUTPUT ?=

.PHONY: install test validate run render-asterisk preflight preflight-strict \
	twilio-config twilio-plan twilio-apply twilio-verify backup package

install:
	$(PYTHON) -m pip install -e '.[test]'

test:
	$(PYTHON) -m pytest -q

validate:
	$(PYTHON) -m compileall -q app scripts tests
	bash -n scripts/*.sh
	$(PYTHON) scripts/validate_configs.py
	node --check web/app.js

run:
	GATE_ENABLED=false OUTBOUND_ENABLED=false AMI_ENABLED=false $(PYTHON) -m app.main

render-asterisk:
	$(PYTHON) scripts/render_asterisk.py

preflight:
	$(PYTHON) scripts/preflight.py --env-file $(RUNTIME_ENV)

preflight-strict:
	$(PYTHON) scripts/preflight.py --env-file $(RUNTIME_ENV) --env-file $(TWILIO_PROVISIONING_ENV) --require-provisioning --strict

twilio-config:
	$(PYTHON) scripts/twilio_bootstrap.py --env-file $(RUNTIME_ENV) --env-file $(TWILIO_PROVISIONING_ENV) show-config

twilio-plan:
	$(PYTHON) scripts/twilio_bootstrap.py --env-file $(RUNTIME_ENV) --env-file $(TWILIO_PROVISIONING_ENV) plan

twilio-apply:
	$(PYTHON) scripts/twilio_bootstrap.py --env-file $(RUNTIME_ENV) --env-file $(TWILIO_PROVISIONING_ENV) apply --yes

twilio-verify:
	$(PYTHON) scripts/twilio_bootstrap.py --env-file $(RUNTIME_ENV) --env-file $(TWILIO_PROVISIONING_ENV) verify

backup:
	$(PYTHON) scripts/backup.py $(if $(BACKUP_OUTPUT),--output-dir $(BACKUP_OUTPUT),)

package:
	mkdir -p dist
	git archive --format=zip --output=dist/floodman-operations-voice-aio.zip HEAD
