# Единая точка входа. Локально: make venv && make lint && make smoke-local
SHELL := /bin/bash
VENV  := .venv
PY    := $(VENV)/bin/python
TF    := terraform -chdir=terraform/yandex
RUN_ID ?= $(shell date +%Y%m%d_%H%M%S)
PROMETHEUS_URL ?=            # http://<loadgen_ip>:9090 через ssh-туннель, см. README

.PHONY: venv ref-audio proto lint lint-python lint-terraform lint-ansible \
        smoke-local up inventory deploy test report down clean

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt -r requirements-dev.txt
	$(VENV)/bin/ansible-galaxy collection install -r ansible/requirements.yml >/dev/null

ref-audio:
	$(PY) scripts/gen_ref_audio.py

proto: ## перегенерировать gRPC-стабы из loadgen/proto/cosyvoice.proto
	$(PY) -m pip install -q -r requirements-grpc.txt
	$(PY) -m grpc_tools.protoc -Iloadgen/proto \
		--python_out=loadgen/proto --grpc_python_out=loadgen/proto loadgen/proto/cosyvoice.proto

# ---------------------------------------------------------------- lint & test
lint: lint-python lint-terraform lint-ansible
	$(PY) -m pytest

lint-python:
	$(VENV)/bin/ruff check loadgen mock_server report tests scripts

lint-terraform:
	@if command -v terraform >/dev/null; then \
		$(TF) init -backend=false -input=false >/dev/null && $(TF) validate && \
		terraform fmt -check -recursive terraform/; \
	else echo "WARN: terraform не установлен — validate пропущен"; fi

lint-ansible:
	@if [ -x $(VENV)/bin/ansible-lint ]; then \
		cd ansible && ../$(VENV)/bin/ansible-lint site.yml; \
	else echo "WARN: ansible-lint не установлен — пропущен"; fi

# ------------------------------------------------------- локальная проверка
smoke-local: ref-audio ## mock-сервер + squeezed-матрица + отчёт (без GPU)
	./scripts/smoke_local.sh $(RUN_ID)

# ------------------------------------------------------------------- облако
up: ## terraform apply + ansible deploy
	$(TF) init -input=false
	$(TF) apply
	$(MAKE) inventory deploy

inventory:
	$(TF) output -json | $(PY) scripts/gen_inventory.py
	$(PY) scripts/gen_group_vars.py

deploy: ## TTS собирается из GitHub (см. config/stand.yaml: tts.repo_url)
	cd ansible && ansible-playbook -i inventory/hosts.ini site.yml

test: ## полная матрица scenarios.yaml на loadgen-ВМ (в фоне, через nohup)
	cd ansible && ansible loadgen -i inventory/hosts.ini -m ansible.builtin.shell -a \
		"cd /opt/tts-loadtest && mkdir -p results && nohup .venv/bin/python -m loadgen.runner \
		 --scenarios config/scenarios.yaml --profile cloud --run-id $(RUN_ID) \
		 > results/$(RUN_ID).log 2>&1 & echo started"
	@echo ">>> Прогон $(RUN_ID) запущен в фоне (~3-4 ч). Статус: make test-status RUN_ID=$(RUN_ID)"
	@echo ">>> НЕ ЗАБУДЬТЕ выключить стенд после report: make down"

test-status: ## статус фонового прогона
	@cd ansible && ansible loadgen -i inventory/hosts.ini -m ansible.builtin.shell -a \
		"tail -8 /opt/tts-loadtest/results/$(RUN_ID).log 2>/dev/null; \
		 pgrep -f 'run-id $(RUN_ID)' >/dev/null && echo '== RUNNING ==' || echo '== FINISHED =='"

report: ## забрать результаты, снять GPU-метрики, собрать report.md
	mkdir -p results
	cd ansible && ansible loadgen -i inventory/hosts.ini -m ansible.builtin.shell -a \
		"cd /opt/tts-loadtest && .venv/bin/python -m report.collect \
		 --run-dir results/$(RUN_ID) --prometheus http://127.0.0.1:9090"
	rsync -az -e "ssh -o StrictHostKeyChecking=accept-new" \
		ubuntu@$$($(TF) output -raw loadgen_public_ip):/opt/tts-loadtest/results/$(RUN_ID) results/
	$(PY) -m report.build_report --run-dir results/$(RUN_ID) --pricing config/pricing.yaml
	@echo ">>> Отчёт: results/$(RUN_ID)/report.md"
	@echo ">>> НЕ ЗАБУДЬТЕ выключить стенд: make down"

down: ## terraform destroy
	$(TF) destroy

clean:
	rm -rf results/smoke_* .pytest_cache .ruff_cache
