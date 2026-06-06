#!/usr/bin/env bash
# setup-branch-protection.sh
#
# Configures GitHub branch protection rules for the 'main' branch via the
# GitHub REST API. Requires a GitHub Personal Access Token with repo scope.
#
# Usage:
#   GITHUB_TOKEN=ghp_... bash scripts/setup-branch-protection.sh
#
# Or export the token first:
#   export GITHUB_TOKEN=ghp_...
#   bash scripts/setup-branch-protection.sh
#
# What this enforces (server-side — applies to ALL contributors):
#   1. The "ci-gate" check must pass before merging (runs all 3 Python versions)
#   2. At least 1 approving review required on every PR
#   3. CODEOWNERS review is required (owner must approve)
#   4. Stale approvals dismissed when new commits are pushed
#   5. Direct pushes to main are blocked — all changes must go via PR
#   6. Admins are NOT exempt (enforced for everyone)

set -euo pipefail

REPO="harshitgindra/PodcastDrive"
BRANCH="main"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    echo "❌  GITHUB_TOKEN is not set."
    echo "    Create a token at: https://github.com/settings/tokens"
    echo "    Required scopes: repo (or administration:write for fine-grained tokens)"
    echo ""
    echo "    Then run:"
    echo "      GITHUB_TOKEN=ghp_... bash scripts/setup-branch-protection.sh"
    exit 1
fi

echo "🔧  Configuring branch protection for '${BRANCH}' on ${REPO}..."

HTTP_STATUS=$(curl --silent --output /tmp/bp_response.json --write-out "%{http_code}" \
    -X PUT \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${REPO}/branches/${BRANCH}/protection" \
    -d '{
        "required_status_checks": {
            "strict": true,
            "contexts": ["ci-gate"]
        },
        "enforce_admins": true,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": true,
            "require_code_owner_reviews": true,
            "required_approving_review_count": 1
        },
        "restrictions": null,
        "allow_force_pushes": false,
        "allow_deletions": false,
        "block_creations": false,
        "required_conversation_resolution": true
    }')

if [[ "$HTTP_STATUS" == "200" ]]; then
    echo ""
    echo "✅  Branch protection rules applied successfully!"
    echo ""
    echo "   Rules now enforced on '${BRANCH}':"
    echo "   • CI must pass (ci-gate — all 3 Python versions)"
    echo "   • At least 1 approving review required"
    echo "   • Code owner review required"
    echo "   • Stale reviews dismissed on new push"
    echo "   • Direct pushes to main are blocked"
    echo "   • Applies to admins too"
else
    echo ""
    echo "❌  Failed to apply branch protection (HTTP ${HTTP_STATUS}):"
    cat /tmp/bp_response.json | python3 -m json.tool 2>/dev/null || cat /tmp/bp_response.json
    echo ""
    echo "Common causes:"
    echo "  • Token lacks 'repo' scope (or 'administration:write' for fine-grained tokens)"
    echo "  • You are not an admin/owner of the repository"
    echo "  • Branch '${BRANCH}' does not exist yet (push at least one commit first)"
    exit 1
fi
