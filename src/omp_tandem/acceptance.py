"""Declared acceptance evidence: what was claimed, what ran, and what is missing.

This module counts. It never decides. A unit with a current passing run is a unit
somebody asserted was exercised and whose run reports a pass against the right
bytes in the right environment — not a criterion that is met. Whether the check
asserts the right behaviour at all is a judgement no schema can make, so
`semantic_sufficiency` and `acceptance` stay `not_assessed` in every response.
"""

from __future__ import annotations

from .models import (
    AcceptanceSet,
    CheckRun,
    CheckScope,
    VerificationPlan,
    assess_checks,
    check_revision,
    run_applies,
)

_PHASES = ("targeted", "candidate", "integration")

# Every dimension a coordinator needs to tell apart. They are deliberately not
# mutually exclusive: one unit can be declared, fail on the required browser and
# pass on older bytes at the same time, and flattening that loses the point.
GAP_CODES = (
    "unmapped",
    "parent_mapping_only",
    "not_selected",
    "declared_unrun",
    "explicit_not_run",
    "current_failure",
    "other_bytes",
    "environment_mismatch",
    "environment_unknown",
    "unknown_set",
    "stale_acceptance_revision",
    "unknown_criterion",
    "unknown_obligation",
    "stale_check_revision",
    "mapping_not_recorded",
    "unmapped_claim",
    "input_mismatch",
    "check_scope_mismatch",
    "unknown_target",
    "target_conflict",
    "unreadable_record",
)


def _key(ref) -> tuple:
    ref = ref if isinstance(ref, dict) else ref.model_dump()
    return (
        ref["set_id"],
        ref["revision"],
        ref["criterion_id"],
        ref.get("obligation_id"),
    )


def _ref(set_id, revision, criterion_id, obligation_id=None) -> dict:
    return {
        "set_id": set_id,
        "revision": revision,
        "criterion_id": criterion_id,
        "obligation_id": obligation_id,
    }


def evidence_units(declared: AcceptanceSet) -> list[dict]:
    """Expand the declared set into the units coverage is actually counted over.

    A criterion that names no obligations is one undecomposed unit; that is a
    declaration about the criterion, not a claim that it has a single clause.
    """
    units = []
    for item in declared.items:
        if not item.obligations:
            units.append(
                {
                    "ref": _ref(declared.set_id, declared.revision, item.id),
                    "text": item.text,
                    "required_environment": {},
                    "decomposition": "not_declared",
                }
            )
            continue
        for duty in item.obligations:
            units.append(
                {
                    "ref": _ref(declared.set_id, declared.revision, item.id, duty.id),
                    "text": duty.text,
                    "required_environment": dict(duty.environment),
                    "decomposition": "declared",
                }
            )
    return units


def _selected(plan: VerificationPlan) -> list:
    ladder = _PHASES[: _PHASES.index(plan.stage) + 1]
    return [check for check in plan.checks if check.phase in ladder]


def _scope(value):
    """A scope arrives as a model from a live report and as a dict from a saved one."""
    if value is None or isinstance(value, CheckScope):
        return value
    return CheckScope.model_validate(value)


def _target(plan: VerificationPlan, report_scope) -> dict:
    """Which bytes the evidence must be about, and how confidently we know them."""
    declared = [
        ("plan_declared", plan.coverage_scope if plan else None),
        ("report_declared", _scope(report_scope)),
    ]
    present = [(source, scope) for source, scope in declared if scope is not None]
    if not present:
        return {"scope": None, "source": "unknown", "conflict": False}
    kinds = {(scope.kind, scope.digest) for _, scope in present}
    if len(kinds) > 1:
        # Two claims about the same bytes that disagree are not a majority vote.
        return {
            "scope": present[0][1].model_dump(),
            "source": present[0][0],
            "conflict": True,
        }
    return {
        "scope": present[0][1].model_dump(),
        "source": present[0][0],
        "conflict": False,
    }


def _answered(records) -> set:
    """Failures the existing assessment counts as answered, over one unit's evidence."""
    resolved = set()
    for row in assess_checks(records)["known_issues"]:
        resolved.add(row["failed_run_id"])
    return resolved


def _environment(required: dict, observed: dict) -> str:
    """A key nobody recorded is unknown, never a match."""
    if not required:
        return "matches"
    for name, value in required.items():
        if name not in observed:
            return "unknown"
        if observed[name] != value:
            return "mismatch"
    return "matches"


def _known(declared: AcceptanceSet | None, ref) -> str:
    """Why a reference cannot be resolved, named precisely rather than 'invalid'."""
    set_id, revision, criterion_id, obligation_id = _key(ref)
    if declared is None or set_id != declared.set_id:
        return "unknown_set"
    if revision != declared.revision:
        return "stale_acceptance_revision"
    item = next((row for row in declared.items if row.id == criterion_id), None)
    if item is None:
        return "unknown_criterion"
    if obligation_id is not None and not any(
        duty.id == obligation_id for duty in item.obligations
    ):
        return "unknown_obligation"
    return "resolved"


def project(
    declared: AcceptanceSet | None,
    plan: VerificationPlan | None,
    runs,
    *,
    task_id: str | None = None,
    report_scope=None,
) -> dict:
    """The whole coverage picture for one task, computed from declarations alone."""
    subject = {"kind": "task", "task_id": task_id}
    base = {
        "schema_version": 1,
        "subject": subject,
        "admission": "not_enforced",
        "semantic_sufficiency": "not_assessed",
        "acceptance": "not_assessed",
    }
    if declared is None:
        # Strings alone are statements, not a denominator. Inventing identities for
        # them would accuse every existing caller of missing mappings it never made.
        return {
            **base,
            "assessment": "not_declared",
            "reason": "acceptance_set_absent",
            "denominator": None,
        }

    records = [
        run if isinstance(run, CheckRun) else CheckRun.model_validate(run)
        for run in runs
    ]
    mode = plan.acceptance_coverage if plan else "report_only"
    selected = _selected(plan) if plan else []
    declarations = {check.id: check for check in (plan.checks if plan else [])}
    revisions = {name: check_revision(check) for name, check in declarations.items()}
    selected_ids = {check.id for check in selected}
    target = _target(plan, report_scope) if plan else _target(None, report_scope)

    declared_for, parents_for = {}, {}
    for check in plan.checks if plan else []:
        for ref in check.acceptance_refs:
            declared_for.setdefault(_key(ref), []).append(check.id)
            set_id, revision, criterion_id, obligation_id = _key(ref)
            if obligation_id is None:
                parents_for.setdefault((set_id, revision, criterion_id), []).append(
                    check.id
                )

    gaps, units = [], []
    for unit in evidence_units(declared):
        key = _key(unit["ref"])
        parent = (key[0], key[1], key[2])
        mapped = sorted(set(declared_for.get(key, [])))
        parent_only = sorted(set(parents_for.get(parent, []))) if key[3] else []
        row = {
            "ref": unit["ref"],
            "text": unit["text"],
            "decomposition": unit["decomposition"],
            "required_environment": unit["required_environment"],
            "mapping": "mapped"
            if mapped
            else "parent_mapping_only"
            if parent_only
            else "unmapped",
            "declared_check_ids": mapped,
            "selected_check_ids": [name for name in mapped if name in selected_ids],
            "current_passing_run_ids": [],
            "current_failed_run_ids": [],
            "answered_failure_run_ids": [],
            "explicit_not_run_ids": [],
            "other_bytes_run_ids": [],
            "environment_mismatch_run_ids": [],
            "environment_unknown_run_ids": [],
            "stale_reference_run_ids": [],
            "unmapped_claim_run_ids": [],
            "input_mismatch_run_ids": [],
            "check_scope_mismatch_run_ids": [],
            "mapping_unrecorded_run_ids": [],
            "unknown_target_run_ids": [],
        }
        applicable = []
        for run in records:
            claims = [ref for ref in run.acceptance_refs if _key(ref) == key]
            if not claims:
                # A run of a mapped check that recorded no mapping is history with a
                # gap in it, not evidence for this unit and not a failure either.
                if run.check_id in mapped and not run.acceptance_refs:
                    row["mapping_unrecorded_run_ids"].append(run.run_id)
                continue
            if run.check_id not in mapped:
                # A run may claim whatever it likes; credit needs the declaration too.
                # Without this a check mapped only to the parent could be handed runs
                # naming its children, and the parent-covers-nothing rule would be
                # worth nothing.
                row["unmapped_claim_run_ids"].append(run.run_id)
                continue
            if revisions.get(run.check_id) != run.check_revision:
                row["stale_reference_run_ids"].append(run.run_id)
                continue
            declared_check = declarations[run.check_id]
            if (run.criterion, run.command) != (
                declared_check.criterion,
                declared_check.command,
            ):
                # The revision says which declaration was observed; it says nothing
                # about what the run actually did. A record whose command or
                # criterion differs from that declaration is not evidence for it.
                row["input_mismatch_run_ids"].append(run.run_id)
                continue
            if run.check_id not in selected_ids:
                continue
            if target["scope"] is None or target["conflict"]:
                row["unknown_target_run_ids"].append(run.run_id)
                continue
            if (run.scope.kind, run.scope.digest) != (
                target["scope"]["kind"],
                target["scope"]["digest"],
            ):
                row["other_bytes_run_ids"].append(run.run_id)
                continue
            if declared_check.scope is not None and not run_applies(
                run, declared_check.scope
            ):
                # The check names the bytes it is about, and the existing
                # verification gate holds runs to it. Coverage must hold them to
                # the same one, or the two gates can pass on different runs.
                row["check_scope_mismatch_run_ids"].append(run.run_id)
                continue
            fit = _environment(unit["required_environment"], run.environment)
            if fit == "unknown":
                row["environment_unknown_run_ids"].append(run.run_id)
                continue
            if fit == "mismatch":
                row["environment_mismatch_run_ids"].append(run.run_id)
                continue
            applicable.append(run)

        # Whether a failure was answered is decided among this unit's own evidence.
        # Sharing the resolution rule does not make the cohorts interchangeable: a
        # pass recorded for a sibling obligation is not an answer to this one.
        answered = _answered(applicable)
        for run in applicable:
            if run.result == "passed":
                row["current_passing_run_ids"].append(run.run_id)
            elif run.result == "failed":
                row[
                    "answered_failure_run_ids"
                    if run.run_id in answered
                    else "current_failed_run_ids"
                ].append(run.run_id)
            else:
                row["explicit_not_run_ids"].append(run.run_id)

        for code, condition in (
            ("unmapped", row["mapping"] == "unmapped"),
            ("parent_mapping_only", row["mapping"] == "parent_mapping_only"),
            ("not_selected", bool(mapped) and not row["selected_check_ids"]),
            (
                "declared_unrun",
                bool(row["selected_check_ids"])
                and not (
                    row["current_passing_run_ids"]
                    or row["current_failed_run_ids"]
                    or row["explicit_not_run_ids"]
                ),
            ),
            ("explicit_not_run", bool(row["explicit_not_run_ids"])),
            ("current_failure", bool(row["current_failed_run_ids"])),
            ("other_bytes", bool(row["other_bytes_run_ids"])),
            ("environment_mismatch", bool(row["environment_mismatch_run_ids"])),
            ("environment_unknown", bool(row["environment_unknown_run_ids"])),
            ("stale_check_revision", bool(row["stale_reference_run_ids"])),
            ("mapping_not_recorded", bool(row["mapping_unrecorded_run_ids"])),
            ("unmapped_claim", bool(row["unmapped_claim_run_ids"])),
            ("input_mismatch", bool(row["input_mismatch_run_ids"])),
            ("check_scope_mismatch", bool(row["check_scope_mismatch_run_ids"])),
            ("unknown_target", bool(row["unknown_target_run_ids"])),
        ):
            if condition:
                gaps.append({"code": code, "acceptance_ref": unit["ref"]})
        units.append(row)

    for check in plan.checks if plan else []:
        for ref in check.acceptance_refs:
            reason = _known(declared, ref)
            if reason != "resolved":
                gaps.append(
                    {
                        "code": reason,
                        "acceptance_ref": ref.model_dump(),
                        "check_id": check.id,
                    }
                )
    if target["conflict"]:
        gaps.append({"code": "target_conflict", "acceptance_ref": None})

    covered = [
        unit
        for unit in units
        if unit["current_passing_run_ids"] and not unit["current_failed_run_ids"]
    ]
    return {
        **base,
        "assessment": "assessed",
        "mode": mode,
        "acceptance_sets": [{"set_id": declared.set_id, "revision": declared.revision}],
        "selected_stage": plan.stage if plan else None,
        "target": {
            "scope": target["scope"],
            "source": target["source"],
        },
        "denominator": {
            "criteria": len(declared.items),
            "evidence_units": len(units),
        },
        "units": units,
        "checks": [
            {
                "check_id": check.id,
                "check_revision": revisions[check.id],
                "selection": "selected" if check.id in selected_ids else "not_selected",
            }
            for check in (plan.checks if plan else [])
        ],
        "gaps": gaps,
        "admission": _admission(mode, units, gaps, len(units), len(covered)),
    }


def _admission(mode, units, gaps, total, covered) -> str:
    if mode != "require_current_evidence":
        return "not_enforced"
    if not total:
        # Nothing declared is not everything covered.
        return "not_assessable"
    return "eligible" if covered == total else "ineligible"


def enforce_coverage(declared, plan, report) -> None:
    """Refuse a success whose declared units lack applicable current evidence."""
    if plan is None or plan.acceptance_coverage != "require_current_evidence":
        return
    if report.outcome != "success":
        return
    coverage = project(
        declared, plan, report.check_runs, report_scope=report.verification_scope
    )
    if coverage["admission"] == "eligible":
        return
    if coverage["assessment"] != "assessed":
        raise ValueError(
            "require_current_evidence needs an acceptance_set; declare one or use report_only"
        )
    open_units = [
        unit["ref"]["criterion_id"]
        + ("/" + unit["ref"]["obligation_id"] if unit["ref"]["obligation_id"] else "")
        for unit in coverage["units"]
        if not unit["current_passing_run_ids"] or unit["current_failed_run_ids"]
    ]
    raise ValueError(
        "Success needs applicable current evidence for every declared unit; "
        f"still open: {sorted(open_units)[:20]}"
    )
