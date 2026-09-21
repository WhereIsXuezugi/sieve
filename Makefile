# Conveniences. Everything here is a one-liner you could type yourself.
.PHONY: help install dev lint test check demo serve assets clean

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-10s %s\n", $$1, $$2}'

install:  ## Install the package
	pip install -e .

dev:      ## Install with test and lint tools
	pip install -e '.[dev]'

lint:     ## Run ruff
	ruff check .

test:     ## Run the test suite
	pytest -q

check: lint test  ## Lint and test, as CI does

demo:     ## Build a synthetic catalogue in /tmp and serve it
	SIEVE_DATA_DIR=/tmp/sieve-demo sieve demo
	SIEVE_DATA_DIR=/tmp/sieve-demo sieve serve

serve:    ## Run against your real data directory
	sieve serve

assets:   ## Regenerate the README diagrams
	python docs/assets/build.py

clean:    ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
