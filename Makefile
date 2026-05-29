.PHONY: install-hooks protect test coverage help

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
	python3 -m pytest tests/ --tb=short

## coverage: Run tests with coverage report (fails if < 95%)
coverage:
	python3 -m pytest tests/ \
		--tb=short \
		--cov=src \
		--cov-report=term-missing \
		--cov-fail-under=95

## help: Show this help message
help:
	@grep -E '^## ' Makefile | sed 's/## /  /'
