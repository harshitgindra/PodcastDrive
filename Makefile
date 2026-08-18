.PHONY: install-hooks protect test coverage help e2e-test e2e-test-full e2e-update-gt lint format fix

# Always use the project venv interpreter. A bare `python3` resolves to a uv
# shim on some dev machines, which recreates/destroys .venv as a side effect.
PYTHON ?= .venv/bin/python3

## install-hooks: Install the pre-push git hook (blocks pushes that would fail CI)
install-hooks:
	@echo "Installing git hooks..."
	cp scripts/pre-push .git/hooks/pre-push
	chmod +x .git/hooks/pre-push
	@echo "✅  pre-push hook installed. Runs on every 'git push' (skip with --no-verify)."

## protect: Apply GitHub branch protection rules to 'main' (requires GITHUB_TOKEN)
protect:
	@bash scripts/setup-branch-protection.sh

## test: Run the full test suite
test:
	$(PYTHON) -m pytest tests/ --tb=short

## coverage: Run tests with coverage report (fails if < 95%)
coverage:
	$(PYTHON) -m pytest tests/ \
		--tb=short \
		--cov=src \
		--cov-report=term-missing \
		--cov-fail-under=95

## help: Show this help message
help:
	@grep -E '^## ' Makefile | sed 's/## /  /'

## e2e-test: Run Tier-1 E2E tests (cached transcript, Bedrock + ffmpeg only)
e2e-test:
	./eval/run_e2e_tests.sh

## e2e-test-full: Run Tier-2 E2E tests (full pipeline including AWS Transcribe)
e2e-test-full:
	./eval/run_e2e_tests.sh --full

## e2e-update-gt: Re-run detection and update ground_truth.json baseline
e2e-update-gt:
	./eval/run_e2e_tests.sh --update-gt

## lint: Run ruff linter
lint:
	$(PYTHON) -m ruff check src/ tests/

## format: Auto-format code with ruff
format:
	$(PYTHON) -m ruff format src/ tests/

## fix: Auto-fix linting issues
fix:
	$(PYTHON) -m ruff check --fix src/ tests/
