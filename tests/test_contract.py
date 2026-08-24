"""Per-module contract triad + capabilities + drift gate (contract-pipeline.md §2-3).

stapel-moderation emits its own contract triad — ``docs/schema.json`` (OpenAPI),
``docs/flows.json`` ([], no @flow_step here) and ``docs/errors.json`` — plus
``docs/capabilities.json`` (§2 fourth artifact), from a single-module
``{moderation + core}`` Django instance mounted at ``/moderation/api/v1/``.

reviews is not mounted in stapel-example-monolith yet, so there is no aggregate
slice to diff against for byte-identity — standalone validation
(contract-pipeline.md §9 fallback) substitutes: determinism, self-contained
$ref closure, JWT security on protected ops, canonical-prefix paths.

Regenerate after any change to a serializer/view/url/error key:

    make contract

then commit docs/{schema,flows,errors,capabilities}.json.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PY = sys.version_info[:2]
if _PY != (3, 12):
    _GOT = f"{_PY[0]}.{_PY[1]}"
    pytest.skip(
        "stapel-moderation contract tests require Python 3.12 (the CI/monolith "
        f"pin) — running {_GOT}. drf-spectacular renders component descriptions "
        "(Optional[X] vs X | None) differently across Python minor versions, so "
        "drift/identity checks emitted+compared under any other minor produce "
        "false diffs. Skipping on any non-3.12 interpreter.",
        allow_module_level=True,
    )

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")
# The fifth artifact (badge-canon §3): docs/llms.txt, rendered from
# docs/capabilities.json (+schema/errors/flows) by stapel_tools.llms_txt.
ARTIFACTS = TRIAD + ("capabilities.json", "llms.txt")

#: Must equal the --budget in the Makefile; test_llms_txt_budget_matches_the_makefile
#: is what keeps the two from drifting apart.
LLMS_TXT_BUDGET = 7000


def _emit(out_dir: Path) -> None:
    for module in ("stapel_moderation._codegen", "stapel_moderation._capabilities"):
        subprocess.run(
            [sys.executable, "-m", module, "--out", str(out_dir)],
            cwd=str(REPO),
            check=True,
            capture_output=True,
        )
    # llms.txt is rendered from the REAL committed docs/capabilities.json (not
    # the just-regenerated tmp one) — same as `make contract-check` — so this
    # step also catches a stale llms.txt independently of the loop above.
    # --budget mirrors the Makefile exactly. The ceiling is raised
    # deliberately (see the Makefile comment): this module's usage surface is
    # 56 entries because its contract IS "call these instead of writing your
    # own version". Emitting here with the generator default would fail, and
    # emitting with a DIFFERENT number from the Makefile would make the drift
    # gate green while `make contract` produced something else.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "stapel_tools.llms_txt",
            ".",
            "--out",
            str(out_dir),
            "--budget",
            str(LLMS_TXT_BUDGET),
        ],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )


def test_contract_artifacts_committed():
    for name in ARTIFACTS:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"
    assert (DOCS / "capabilities.meta.json").is_file(), (
        "missing docs/capabilities.meta.json — the curated layer is "
        "hand-written and committed, not generated"
    )


def test_contract_has_no_drift(tmp_path):
    _emit(tmp_path)
    for name in ARTIFACTS:
        committed = (DOCS / name).read_bytes()
        regenerated = (tmp_path / name).read_bytes()
        assert committed == regenerated, (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in ARTIFACTS:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    schema = json.loads((DOCS / "schema.json").read_text())
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith("/moderation/api/v1/") for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /moderation/api/v1/ prefix"
    )


def test_flows_are_empty_no_flow_step_annotations():
    flows = json.loads((DOCS / "flows.json").read_text())
    assert flows == [], (
        "docs/flows.json is non-empty but no @flow_step annotation exists in "
        "stapel_moderation — investigate before assuming [] is still correct"
    )


def test_the_public_disclosure_is_the_only_unauthenticated_operation():
    """Everything else is either a member surface or the staff console. The
    disclosure is public deliberately: a transparency page that requires an
    account is not one."""
    schema = json.loads((DOCS / "schema.json").read_text())
    public = []
    for path, operations in schema["paths"].items():
        for method, op in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            security = op.get("security") or []
            if not any("JWTCookieAuth" in entry for entry in security):
                public.append(f"{method.upper()} {path}")
    assert public == ["GET /moderation/api/v1/policy"], public


def _all_refs(obj) -> set[str]:
    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


def test_schema_refs_are_self_contained():
    schema = json.loads((DOCS / "schema.json").read_text())
    comps = schema.get("components", {}).get("schemas", {})
    seen: set[str] = set()
    stack = list(_all_refs(schema["paths"]))
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in comps:
            stack.extend(_all_refs(comps[name]))
    dangling = seen - set(comps)
    assert not dangling, f"dangling $ref(s) with no component definition: {dangling}"


#: The single route that is anonymous on purpose (see the test below it).
PUBLIC_OPERATIONS = {"GET /moderation/api/v1/policy"}


def test_protected_paths_carry_jwt_security():
    schema = json.loads((DOCS / "schema.json").read_text())
    missing = []
    for path, operations in schema["paths"].items():
        for method, op in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            if f"{method.upper()} {path}" in PUBLIC_OPERATIONS:
                continue
            security = op.get("security") or []
            if not any("JWTCookieAuth" in entry for entry in security):
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"operations missing JWTCookieAuth security: {missing}"


# --- capabilities.json content sanity (capability-config.md §2) ---------------


def _capabilities() -> dict:
    return json.loads((DOCS / "capabilities.json").read_text())


def test_capabilities_axes_are_the_settings_that_change_the_deal():
    """Eleven axes, and every one of them changes what the platform DOES to
    people. Timeouts, page sizes, media caps, throttles and lease seconds are
    deliberately absent: they bound cost and abuse, they do not change the
    deal with anybody."""
    axes = {a["key"]: a for a in _capabilities()["axes"]}
    assert set(axes) == {
        "TARGET_TYPES",
        "SCREEN_ENABLED",
        "SCREENER",
        "ON_SCREENING_FAILURE",
        "ON_SCREENING_UNAVAILABLE",
        "AUTO_RESOLVE_STALE_QUEUE",
        "ALLOW_ANONYMOUS_REPORTS",
        "APPEAL_REQUIRES_DIFFERENT_ACTOR",
        "RETENTION_DAYS",
        "SANCTION_RETENTION_DAYS",
        "WORKSPACE_SCOPED",
    }
    for axis in axes.values():
        # Behavioral, not gating — they change behavior, not which ops exist.
        assert axis["gates"]["operations"] == []
        assert axis["curated"]["business_label"]


def test_the_closed_defaults_are_published_as_closed():
    """The capabilities document is what a reader trusts about a deployment's
    posture, so the closed defaults are asserted HERE too, not only in the
    behavioural tests: a doc that said "approve" while the code held would be
    worse than no doc."""
    axes = {a["key"]: a for a in _capabilities()["axes"]}
    assert axes["ON_SCREENING_FAILURE"]["default"] == "hold"
    assert axes["AUTO_RESOLVE_STALE_QUEUE"]["default"] is None
    assert axes["ALLOW_ANONYMOUS_REPORTS"]["default"] is False
    assert axes["APPEAL_REQUIRES_DIFFERENT_ACTOR"]["default"] is True
    assert axes["WORKSPACE_SCOPED"]["default"] is False


def test_capabilities_extension_points_cover_the_seams():
    names = {e["name"] for e in _capabilities()["extension_points"]}
    assert {"TARGET_TYPES", "REASONS", "RULES", "SCREENER"} <= names


def test_capabilities_operations_total_matches_schema():
    schema = json.loads((DOCS / "schema.json").read_text())
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    total = sum(1 for item in schema["paths"].values() for m in item if m in methods)
    assert _capabilities()["operations_total"] == total


def test_capabilities_envelope():
    doc = _capabilities()
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert doc["module"] == pyproject["project"]["name"]
    assert doc["version"] == pyproject["project"]["version"]
    assert doc["provides"]
    assert doc["extension_points"]
    assert doc["requires"]


# --- README.md — the sixth artifact (tracker #257) ---------------------------
#
# README.md is assembled by ``stapel_tools.readme`` from docs/readme.md (the
# human half: what this module is and how to think about it) plus the contract
# documents above (badges, version, surface counts, doc links). Everything a
# hand-written README used to restate — and therefore used to get wrong one
# release later — is generated and gated here.

def test_readme_is_assembled_and_has_no_drift():
    from stapel_tools.readme import load_inputs, render, static_languages

    inputs = load_inputs(REPO)
    languages = static_languages(REPO)
    assert languages == ["en"], "expected exactly the English static body docs/readme.md"
    committed = (REPO / "README.md").read_text()
    assert committed == render(REPO, inputs, "en", languages), (
        "README.md drifted — run `make contract` and commit README.md "
        "(edit prose in docs/readme.md, never README.md itself)"
    )


def test_readme_version_matches_the_package():
    """The #226 gate, at the point where the number is published."""
    import tomllib

    from stapel_tools.readme import load_inputs, resolve_version

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert resolve_version(load_inputs(REPO)) == pyproject["project"]["version"]


def test_llms_txt_budget_matches_the_makefile():
    """Two places name the budget, so one test makes them one number.

    A drift gate that regenerates under a different ceiling than `make
    contract` uses is a gate that passes on an artifact nobody can reproduce.
    """
    makefile = (REPO / "Makefile").read_text()
    assert f"--budget {LLMS_TXT_BUDGET}" in makefile
    assert "--budget" not in makefile.replace(f"--budget {LLMS_TXT_BUDGET}", "")


def test_the_case_card_declares_its_content_in_the_schema():
    """A key a generated client cannot see is a key it cannot type.

    ``content`` used to be attached to the response dict after serialization,
    so every consumer generating types from this schema had to hand-write the
    one field the whole moderator console is built around.
    """
    schema = json.loads((DOCS / "schema.json").read_text())
    comps = schema["components"]["schemas"]
    card = comps["CaseDetailPresenterDTO"]
    assert "content" in card["properties"], (
        "CaseDetailPresenterDTO has no `content` property — the case card's "
        "content is grafted on after serialization again"
    )
    assert "content" in card["required"]
    assert "ContentDTO" in comps
    assert set(comps["ContentDTO"]["properties"]) >= {
        "available",
        "error",
        "text",
        "title",
        "media",
        "author_id",
    }


def test_every_registered_error_key_carries_a_remediation():
    """A refusal a client cannot act on is a dead end in the UI."""
    from stapel_moderation.errors import (
        STAPEL_MODERATION_ERRORS,
        STAPEL_MODERATION_REMEDIATION,
    )

    missing = set(STAPEL_MODERATION_ERRORS) - set(STAPEL_MODERATION_REMEDIATION)
    assert not missing, f"error keys with no remediation verb: {sorted(missing)}"
