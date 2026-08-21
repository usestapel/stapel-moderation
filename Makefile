# stapel-moderation — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its OWN contract triad (schema.json + flows.json +
# errors.json) from a single-module {moderation + core} Django instance mounted
# at the canonical /moderation/api/v1 prefix (see _codegen.py /
# _codegen_settings.py / codegen_urls.py).
#
# stapel-moderation is not mounted in stapel-example-monolith, so there is no
# aggregate slice to diff these artifacts against for byte-identity —
# validation is standalone (determinism + closure + canonical prefix; see
# tests/test_contract.py, which is the authoritative CI gate). These
# targets are the dev-loop convenience.
#
# PYTHON must have the module + its deps importable (the repo venv, or a
# CI venv). Emission is pinned to Python 3.12: drf-spectacular renders
# component descriptions differently across minors, and a contract emitted
# on the wrong one produces false diffs forever.
PYTHON ?= python3

.PHONY: contract contract-check migration-lint lint test emit-check

# Emit the contract triad + capabilities.json + llms.txt, then assemble
# README.md from docs/readme.md plus everything above.
#
# The llms.txt budget is raised from the generator's default 4000 to 7000 —
# above the 5000 that stapel-forms and stapel-recordings take, below
# stapel-auth's 8000. The measured document is ~6350 tokens and the bulk of
# it is the 56-entry usage surface (2740) plus the 27-key error catalogue
# (912) plus 11 config axes (1105). That size is the module, not padding: a
# moderation service whose whole contract is "call these services instead of
# writing your own version of them" has a large surface by construction, and
# the axes are the settings that decide whether unscreened content reaches
# the public.
#
# Raise the ceiling deliberately; do NOT shorten the `intent` lines in
# docs/capabilities.meta.json to fit, because a trimmed context file reads
# exactly like a complete one at the point of use, which is the failure mode
# the hard budget exists to prevent.
contract:
	$(PYTHON) -m stapel_moderation._codegen --out docs
	$(PYTHON) -m stapel_moderation._capabilities --out docs
	$(PYTHON) -m stapel_tools.llms_txt . --out docs --budget 7000
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate into a temp dir and diff against the committed docs/*.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_moderation._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_moderation._capabilities --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" --budget 7000 || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json capabilities.json llms.txt; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} + README.md up to date"; fi; \
	exit $$rc

# Expand/contract gate for Django migrations (release-management.md §3).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict $(if $(BASE_SHA),--base-sha $(BASE_SHA),)

# Outbox discipline: an emit that is not inside the mutating transaction,
# or one whose failure is swallowed, is a row that exists without the fact
# it announced.
emit-check:
	$(PYTHON) -m stapel_core.lint.emit_check .

lint:
	ruff check . --select E,F,W --ignore E501

test:
	$(PYTHON) -m pytest tests/ -q
