"""Explicit cumulative verification declarations and current-report admission.

This module never runs commands or changes grants. Historical runs remain evidence,
not current passing claims.
"""

from .models import CheckRun, VerificationPlan, assess_checks, check_revision

_PHASES = ("targeted", "candidate", "integration")


def verification_requirements(plan) -> dict:
    plan = VerificationPlan.model_validate(plan)
    checks = [
        check
        for phase in _PHASES[: _PHASES.index(plan.stage) + 1]
        for check in plan.checks
        if check.phase == phase
    ]
    return {
        "checks": [check.model_dump() for check in checks],
        "estimated_seconds": plan.preparation_seconds
        + sum(check.estimated_seconds or 0 for check in checks),
        "requires_shell": any(
            check.requires_shell or check.command for check in checks
        ),
        "estimates_complete": all(
            check.estimated_seconds is not None for check in checks
        ),
    }


def assess_verification(plan, runs) -> dict:
    """Assess only declared current inputs while retaining the independent history."""
    selected = verification_requirements(plan)["checks"]
    records = [
        run if isinstance(run, CheckRun) else CheckRun.model_validate(run)
        for run in runs
    ]
    grouped = {}
    for run in records:
        grouped.setdefault(run.check_id, []).append(run)
    criteria, issues, invalid = [], [], []
    for check in selected:
        assessed = assess_checks(grouped.get(check["id"], []), check.get("scope"))
        matching = [
            row
            for row in assessed["criteria"]
            if row["criterion"] == check["criterion"]
            and (check.get("scope") is None or row["applicable_run_ids"])
        ]
        if not matching:
            matching = [
                {
                    "criterion": check["criterion"],
                    "status": "not_run",
                    "current_run_id": None,
                }
            ]
        criteria.extend(
            {**row, "check_id": check["id"], "declared_scope": check.get("scope")}
            for row in matching
        )
        issues.extend(assessed["known_issues"])
        invalid.extend(assessed["invalid_supersedes"])
    statuses = {row["status"] for row in criteria}
    return {
        "status": (
            "failed"
            if "failed" in statuses
            else "not_run"
            if not criteria or "not_run" in statuses
            else "passed"
        ),
        "criteria": criteria,
        "known_issues": issues,
        "invalid_supersedes": invalid,
        "run_count": len(records),
        "interpretation": "declared_current_inputs; full history remains in check_runs",
    }


def enforce_verification(plan, report, *, trusted=None) -> None:
    """A success must identify each selected check and its current passing run."""
    if plan is None or report.outcome != "success":
        return
    required = {
        check["id"]: check for check in verification_requirements(plan)["checks"]
    }
    claims = {}
    if trusted is not None:
        if trusted.get("source") != "supervisor_checks_v1":
            raise ValueError("Unknown trusted verification source")
        # A rejection may precede every check, but cannot leave a process active.
        if trusted.get("verdict") == "reject":
            if not trusted.get("teardown_confirmed"):
                raise ValueError("Supervisor verification teardown is not confirmed")
            return
        if not trusted.get("settled") or not trusted.get("comparison_opened"):
            raise ValueError("Supervisor verification and comparison have not settled")
        if trusted.get("verdict") != "accept" or trusted.get("status") != "passed":
            raise ValueError("Successful review requires its trusted passing checks")
        if set(trusted.get("check_ids", [])) != required.keys():
            raise ValueError("Trusted verification does not match the selected ladder")
        observed = {
            run.check_id: run
            for value in trusted.get("runs", [])
            for run in [CheckRun.model_validate(value)]
        }
        for identifier, check in required.items():
            run = observed.get(identifier)
            if (
                run is None
                or run.result != "passed"
                or run.provenance != "machine_observed"
                or run.criterion != check["criterion"]
                or run.command != check["command"]
                or run.check_revision != check_revision(check)
            ):
                raise ValueError(
                    f"Missing current supervisor observation for check {identifier}"
                )
        return
    for claim in report.checks:
        if claim.check_id is not None:
            if claim.check_id in claims:
                raise ValueError("Duplicate current verification check_id")
            claims[claim.check_id] = claim
    missing = required.keys() - claims.keys()
    if missing:
        raise ValueError(
            f"Success omits required verification checks: {sorted(missing)}"
        )
    runs = {}
    for run in report.check_runs:
        if run.run_id in runs:
            raise ValueError("Duplicate verification run_id")
        runs[run.run_id] = run
    assessment = assess_verification(plan, report.check_runs)
    for identifier, check in required.items():
        claim = claims[identifier]
        run = runs.get(claim.run_id)
        current = next(
            (
                item
                for item in assessment["criteria"]
                if run is not None
                and item["check_id"] == identifier
                and item.get("role") == run.role
            ),
            None,
        )
        if (
            claim.result != "passed"
            or assessment["status"] != "passed"
            or run is None
            or run.check_id != identifier
            or current is None
            or current["current_run_id"] != run.run_id
            or current["status"] != "passed"
            or run.result != "passed"
            or run.criterion != check["criterion"]
            or run.command != check["command"]
            or (claim.command is not None and claim.command != check["command"])
        ):
            raise ValueError(
                f"Success requires a current passing run for verification check {identifier}; "
                "retain earlier failures in check_runs and reference the current run_id"
            )
