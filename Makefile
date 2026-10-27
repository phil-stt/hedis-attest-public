PY ?= .venv/bin/python

.PHONY: dev dev-down venv test lint data data-v2 eval eval-heldout

dev:             ## the whole app in Docker on :8080 (needs Docker; .env optional)
	docker compose up --build

dev-down:
	docker compose down

venv:
	python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check attest tests scripts

data:             ## v1 dataset (data/*.json)
	$(PY) scripts/generate_data.py

data-v2:          ## v2 eval block (committed) and, with GAPS=N, a population bundle (gitignored)
	$(PY) scripts/generate_data_v2.py $(if $(GAPS),--gaps $(GAPS),)

eval:            ## dev split: use for prompt work
	$(PY) -m attest.eval --split dev

eval-report:     ## the held-out eval of record, from the committed run files (no API key, no spend)
	$(PY) -m attest.eval show

eval-heldout:    ## the eval of record: never tune against it
	$(PY) -m attest.eval --split heldout

.PHONY: run seed-load seed-reset test-postgres pipeline-report

pipeline-report: ## per-tranche cost, cache and router-recall report for the store's jobs
	$(PY) -m attest.pipeline report

run:             ## local app on :8080 (set DEMO_PASSWORD in .env to require the password)
	.venv/bin/uvicorn attest.app:app --port 8080 --reload

seed-load:       ## dataset + seed/findings.json into the store (DATABASE_URL or state/attest.db)
	$(PY) -m attest.seed load

test-postgres:   ## the suite against Postgres, in its own database (needs: docker compose up -d db)
	DATABASE_URL=postgresql+psycopg://attest:attest@localhost:5432/attest_test $(PY) -m pytest -q

seed-reset:      ## clear reviewer decisions and records (known demo state)
	$(PY) -m attest.seed reset
