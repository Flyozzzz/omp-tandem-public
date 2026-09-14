"""Immutable conversation policy and per-turn worker message construction."""

import json
from pathlib import Path

from .models import (
    ContextOptions,
    ConversationHandoff,
    TaskRequirements,
    VerificationPlan,
    WorkPolicy,
)
from .project_context import ProjectContextStore, task_context_packet
from .reviews import ReviewStore
from .verification import verification_requirements
from .workspace import ProjectScope


def owned_paths(cwd, contract):
    if not contract:
        return set()
    root = Path(cwd).resolve()
    paths = {(root / value).resolve() for value in contract.scope.owned_files}
    if any(
        not path.is_relative_to(root) or path == root or path.is_dir() for path in paths
    ):
        raise ValueError(
            "Owned files must be files inside cwd, including resolved symlinks"
        )
    return paths


def work_policy(task):
    if task["policy_json"]:
        return WorkPolicy.model_validate_json(task["policy_json"])
    # Old conversations stored their persistent policy inside the first contract.
    legacy = json.loads(task["contract_json"]) if task["contract_json"] else {}
    return WorkPolicy(
        scope=legacy.get("scope", {}), constraints=legacy.get("constraints", [])
    )


def current_task(task):
    contract = json.loads(task["contract_json"]) if task["contract_json"] else {}
    verification = (
        VerificationPlan.model_validate(contract["verification"])
        if contract.get("verification") is not None
        else None
    )
    return {
        "goal": contract.get("goal", task["prompt"]),
        "context": contract.get("context", ""),
        "constraints": contract.get("constraints", []),
        "acceptance": contract.get("acceptance", []),
        "artifact_ids": contract.get("artifact_ids", []),
        "context_options": ContextOptions.model_validate(
            contract.get("context_options", {})
        ).model_dump(mode="json"),
        "requirements": TaskRequirements.model_validate(
            contract.get("requirements", {})
        ).model_dump(mode="json"),
        "verification": (
            {"stage": verification.stage, **verification_requirements(verification)}
            if verification is not None
            else None
        ),
    }


def require_review_context_capture(review_id, review_stage, project_context_id):
    if review_id and review_stage != "comparison" and project_context_id:
        raise ValueError(
            "Independent snapshot review cannot bind product context; recapture all "
            "required requirements, policy and context in ReviewStore before dispatch"
        )


class TaskMessages:
    def __init__(
        self, scope: ProjectScope, projects: ProjectContextStore, reviews: ReviewStore
    ):
        self.scope = scope
        self.projects = projects
        self.reviews = reviews

    def build(self, task, snapshot=None):
        require_review_context_capture(
            task.get("review_id"),
            task.get("review_stage"),
            task.get("project_context_id") or snapshot,
        )
        policy = work_policy(task)
        current = current_task(task)
        inherited = set(policy.constraints)
        current["constraints"] = [
            value for value in current["constraints"] if value not in inherited
        ]
        if snapshot is None and task["project_context_id"]:
            snapshot = self.projects.get(task["project_context_id"])
        if snapshot is not None and snapshot["context_id"] != task.get(
            "project_context_id"
        ):
            raise ValueError("Worker context must match the task's pinned snapshot")
        project = task_context_packet(
            snapshot,
            current["context_options"],
            unchanged=bool(task.get("context_unchanged"))
            and not task.get("handoff_json"),
        )
        handoff = None
        if task.get("handoff_json"):
            handoff = {
                "attribution": "Operator-supplied summary for an explicitly fresh conversation; not verified evidence or execution authority.",
                "previous_task_id": task.get("previous_task_id"),
                "previous_conversation_id": task.get("previous_conversation_id"),
                "data": ConversationHandoff.model_validate_json(
                    task["handoff_json"]
                ).model_dump(mode="json"),
            }
        review = None
        if task.get("review_id"):
            review = {
                **self.reviews.info(task["review_id"]),
                "stage": task["review_stage"],
                "material_access": "Read saved material only through tandem_review_read. The live working directory is not this snapshot.",
                "author_access": (
                    "Compare the revealed author proposal against the recorded independent assessment. Explain revisions."
                    if task["review_stage"] == "comparison"
                    else "Author proposal and rationale are withheld. Read requirements, criteria and saved code; formulate the problem independently first. Clarification cannot add free text to this stage: tandem_ask returns clarification_requires_new_snapshot, and the stage must end blocked/partial. Put exact missing paths in context as JSON requested_paths; a new capture/attempt is required."
                ),
                "exposure_limit": "Code, earlier conversation or supplied context may already expose a solution; do not claim a blind review after prior exposure.",
            }
        return json.dumps(
            {
                "workspace": {
                    **self.scope.info(),
                    "allowed_roots": json.loads(task["workspace_roots"])
                    if task["workspace_roots"]
                    else [str(self.scope.root)],
                },
                "work_policy": {
                    "mode": task["mode"],
                    "cwd": task["cwd"],
                    "question_timeout_seconds": task["question_timeout_seconds"],
                    **policy.model_dump(),
                },
                "task": current,
                "project_context": project,
                "review": review,
                "handoff": handoff,
                "replaces_project_context_id": task["previous_project_context_id"],
            },
            ensure_ascii=False,
        )
