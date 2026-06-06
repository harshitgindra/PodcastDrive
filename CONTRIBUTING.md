# Contributing to PodcastDrive

Thank you for considering a contribution! This document describes how to set up the development environment, run tests, and submit a pull request.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local setup](#local-setup)
3. [Running tests](#running-tests)
4. [Git hooks](#git-hooks)
5. [Code style](#code-style)
6. [Pull-request workflow](#pull-request-workflow)
7. [Commit message convention](#commit-message-convention)
8. [Reporting bugs](#reporting-bugs)

---

## Prerequisites

| Tool | Minimum version |
|------|----------------|
| Python | 3.11 |
| pip | 23+ |
| git | 2.x |
| AWS CLI *(optional, for manual S3 testing)* | v2 |

---

## Local setup

```bash
# 1. Clone the repo
git clone https://github.com/harshitgindra/PodcastDrive.git
cd PodcastDrive

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .\.venv\Scripts\Activate.ps1    # Windows PowerShell

# 3. Install all dependencies (runtime + test)
pip install -r requirements.txt

# 4. Copy the example config and fill in your values
cp config.env.example config.env
# Edit config.env — at minimum set S3_BUCKET and CLOUDFRONT_BASE
```

---

## Running tests

All tests live in `tests/` and target the source modules in `src/`.

```bash
# Run the full suite
python3 -m pytest tests/

# Run with coverage report
python3 -m pytest tests/ --cov=src --cov-report=term-missing

# Run a single test file
python3 -m pytest tests/test_sync.py -v

# Run tests matching a keyword
python3 -m pytest tests/ -k "sleep"
```

The CI workflow (`.github/workflows/test.yml`) requires **≥ 95 % coverage** and runs on Python 3.11, 3.12 and 3.13. Please make sure your changes don't drop coverage below this threshold.

---

## Git hooks

A `pre-push` hook is provided that runs the **exact same test command as CI** before every `git push`. This gives you fast local feedback before code reaches GitHub.

> **Note:** The hook is opt-in per machine. Server-side branch protection (below) is the authoritative enforcement that covers all contributors regardless of local setup.

### Install (one-time setup)

```bash
make install-hooks
```

Or manually:

```bash
cp scripts/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

### What it does

Every time you run `git push`, the hook executes:

```bash
python -m pytest tests/ --tb=short --cov=src --cov-report=term-missing --cov-fail-under=95
```

- ✅ **All tests pass + coverage ≥ 95%** → push proceeds normally.
- ❌ **Tests fail or coverage drops** → push is blocked with a clear error message.

### Bypassing the hook

For work-in-progress branches where you intentionally want to push without running tests:

```bash
git push --no-verify
```

---

## GitHub branch protection (server-side enforcement)

Branch protection rules enforce CI for **every contributor**, regardless of whether they have the local hook installed. These rules are applied server-side on GitHub and cannot be bypassed.

### What is enforced on `main`

| Rule | Effect |
|------|--------|
| **`ci-gate` must pass** | All 3 Python-version test jobs must be green before merge |
| **1 approving review required** | No self-merge; at least one teammate must approve |
| **CODEOWNERS review required** | `@harshitgindra` must approve every PR (see `.github/CODEOWNERS`) |
| **Stale reviews dismissed** | New commits invalidate existing approvals |
| **Direct pushes to `main` blocked** | All changes must go through a PR |
| **Enforced for admins** | No bypass, even for repository owners |

### One-time setup (repository owner only)

Run this once after creating the repository (or after changing the rules):

```bash
# Generate a token at https://github.com/settings/tokens
# Required scope: repo  (classic token)
#                 or administration:write  (fine-grained token)
GITHUB_TOKEN=ghp_... make protect
```

This calls `scripts/setup-branch-protection.sh` which applies all rules above via the GitHub REST API.

### How it protects the team

```
Developer A               GitHub                   GitHub Actions CI
    │                        │                            │
    ├── git push feature ──► │                            │
    │                        ├── PR opened                │
    │                        ├── triggers CI ────────────►│
    │                        │                            ├── Python 3.11 ✅
    │                        │                            ├── Python 3.12 ✅
    │                        │                            ├── Python 3.13 ✅
    │                        │◄─── ci-gate: PASS ─────────┤
    │                        │                            │
    │◄── review required ────┤                            │
    │    (CODEOWNER)         │                            │
    │                        │                            │
    ├── approved + CI green ►│                            │
    │                        ├── merge allowed ✅          │
```

If CI fails or no review is given, **GitHub blocks the merge button** — no code reaches `main`.

---

## Code style

- **Formatting** — no formatter is enforced yet; please match the surrounding style (4-space indent, double quotes).
- **Type hints** — all public functions must have full type annotations (PEP 484 / 526). Use `X | None` instead of `Optional[X]` (Python 3.10+).
- **Docstrings** — every public function and class must have a Google-style docstring with `Args:` and `Returns:` sections.
- **No bare `except:`** — always catch a specific exception or at minimum `Exception`.

---

## Pull-request workflow

1. **Fork** the repository and create a feature branch:

   ```bash
   git checkout -b feat/my-feature
   ```

2. Make your changes in small, focused commits (see [Commit message convention](#commit-message-convention)).

3. Run the full test suite and ensure all tests pass with no coverage regression.

4. Open a pull request against `main`. Fill in the PR template with:
   - What the change does and why
   - How to test it manually (if applicable)
   - Any follow-up work deferred to later issues

5. Address review comments. Once approved and CI is green, a maintainer will merge.

---

## Commit message convention

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short summary>

<optional body — wrap at 72 chars>

<optional footer — e.g. Closes #42>
```

**Common types:**

| Type | When to use |
|------|------------|
| `feat` | New feature or user-visible behaviour |
| `fix` | Bug fix |
| `test` | Adding or fixing tests (no production code change) |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `docs` | Documentation only |
| `chore` | Build process, dependencies, CI config |
| `perf` | Performance improvement |

---

## Reporting bugs

Please [open an issue](https://github.com/harshitgindra/PodcastDrive/issues/new) and include:

- Python version (`python3 --version`)
- A minimal reproduction (playlist URL can be anonymised)
- The full traceback or log output
- Expected vs. actual behaviour
