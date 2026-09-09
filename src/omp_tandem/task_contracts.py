"""Immutable conversation policy and per-turn worker message construction."""

import json
from pathlib import Path

from .models import WorkPolicy
from .project_context import ProjectContextStore
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
    return {
        "goal": contract.get("goal", task["prompt"]),
        "context": contract.get("context", ""),
        "constraints": contract.get("constraints", []),
        "acceptance": contract.get("acceptance", []),
        "artifact_ids": contract.get("artifact_ids", []),
    }


class TaskMessages:
    def __init__(self, scope: ProjectScope, projects: ProjectContextStore):
        self.scope = scope
        self.projects = projects

    def build(self, task, snapshot=None):
        policy = work_policy(task)
        current = current_task(task)
        inherited = set(policy.constraints)
        current["constraints"] = [
            value for value in current["constraints"] if value not in inherited
        ]
        if snapshot is None and task["project_context_id"]:
            snapshot = self.projects.get(task["project_context_id"])
        project = None
        if snapshot is not None:
            project = {
                key: snapshot[key]
                for key in ("context_id", "project_id", "revision", "sha256", "context")
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
                "replaces_project_context_id": task["previous_project_context_id"],
            },
            ensure_ascii=False,
        )
