"""Durable team scheduling through the deterministic native RPC boundary."""

import asyncio
import copy
import json
import threading
from contextlib import closing
from uuid import uuid4

from omp_tandem.team_runs import ACTIVE_TEAM, TeamRuns
from omp_tandem.workspace import TaskAdmissionBusy
from tests.helpers import PEER, RpcHarness

TEAM_PEER = (
    PEER.replace(
        "'model': None",
        "'model': {'provider':'openai-codex','id':'baseline','name':'Local','api':'local','baseUrl':'http://127.0.0.1','reasoning':True,'thinking':{'mode':'effort','efforts':['high']},'input':['text'],'contextWindow':200000,'maxTokens':20000,'cost':{'input':0,'output':0}}",
    )
    .replace(
        "        if scenario == 'missing-report':",
        """        if scenario.startswith('team:'):
            import time
            name = scenario.split(':', 1)[1]
            root = Path(__file__).parent
            context = task['task'].get('context', '')
            config = json.JSONDecoder().raw_decode(context or '{}')[0]
            (root / (name + '.entered')).write_text('entered')
            if config.get('gate'):
                until = time.monotonic() + 15
                while not (root / (name + '.release')).exists() and time.monotonic() < until:
                    time.sleep(0.01)
            if config.get('write'):
                with (root / config['write']).open('a') as target:
                    target.write(name + '\\n')
            evidence = json.loads(context.rsplit('\\n', 1)[-1]) if 'Team predecessor evidence' in context else []
            answer = name + ':' + ','.join(item['summary'] for item in evidence)
            report = {'outcome': config.get('outcome', 'success'), 'summary': name, 'answer': answer}
            if report['outcome'] == 'partial':
                report.update(checks=[{'name': 'source coverage', 'result': 'not_run'}], blockers=['One source remains unavailable'])
            if config.get('source'):
                emit({'type': 'host_tool_call', 'id': 'team-source', 'toolCallId': 'team-source-call', 'toolName': 'tandem_publish_artifact', 'arguments': {'name': 'source-note', 'content': config['source']}})
            elif config.get('read_reports'):
                recovered = {'reports': [], 'sources': []}
                reads = [('reports', item['outcome_artifact_id']) for item in evidence]
                target, artifact_id = reads.pop(0)
                emit({'type': 'host_tool_call', 'id': 'team-read', 'toolCallId': 'team-read-call', 'toolName': 'tandem_read_artifact', 'arguments': {'artifact_id': artifact_id}})
            else:
                finish(report)
        elif scenario == 'missing-report':""",
    )
    .replace(
        "    elif kind == 'host_tool_result' and command['id'] == 'question':",
        """    elif kind == 'host_tool_result' and command['id'] == 'team-source':
        response = json.loads(command['result']['content'][0]['text'])
        report['artifact_ids'] = [response['artifact_id']]
        finish(report)
    elif kind == 'host_tool_result' and command['id'] == 'team-read':
        response = json.loads(command['result']['content'][0]['text'])
        if command.get('isError'):
            end('Could not recover predecessor evidence: ' + str(response))
            continue
        content = response['content']
        if target == 'reports':
            content = json.loads(content)
            reads.extend(('sources', identifier) for identifier in content.get('artifact_ids', []))
        recovered[target].append(content)
        if reads:
            target, artifact_id = reads.pop(0)
            emit({'type': 'host_tool_call', 'id': 'team-read', 'toolCallId': 'team-read-' + artifact_id, 'toolName': 'tandem_read_artifact', 'arguments': {'artifact_id': artifact_id}})
        else:
            report['answer'] = json.dumps(recovered)
            finish(report)
    elif kind == 'host_tool_result' and command['id'] == 'question':""",
    )
)


class AgreedModels:
    """Offline catalog agreement; workers still execute through the real runtime."""

    def __init__(self, pending=False):
        self.pending = pending
        self.calls = 0
        self.entered = threading.Event()
        self.release = None

    def select(self, plan, *, deadline, cancelled):
        self.calls += 1
        self.entered.set()
        if self.release is not None:
            while not self.release.wait(0.01):
                if cancelled():
                    break
        pending = {
            step["id"]
            for step in plan["steps"]
            if self.pending is True or (self.pending and step["id"] in self.pending)
        }
        return {
            "status": "needs_assignment" if pending else "ready",
            "catalog": {
                "status": "ready",
                "models": [{"model": "openai-codex/baseline"}],
            },
            "eligible_models": {
                step["id"]: ["openai-codex/baseline"] for step in plan["steps"]
            },
            "eligible_assignments": {
                step["id"]: [{"model": "openai-codex/baseline", "thinking": "high"}]
                for step in plan["steps"]
            },
            "assignments": {
                step["id"]: {
                    "model": "openai-codex/baseline",
                    "thinking": "high",
                    "role": "developer",
                    "source": "coordinator",
                    "reason": "Offline agreed catalog",
                }
                for step in plan["steps"]
                if step["id"] not in pending
            },
            "pending": [step["id"] for step in plan["steps"] if step["id"] in pending],
            "pending_reasons": {
                key: "Explicit coordinator agreement required" for key in pending
            },
            "jev": {
                "status": "disabled",
                "decisions": {"catalog": "Offline agreed catalog"},
            },
        }

    def assign(self, plan, selection, assignments):
        if set(assignments) != set(selection["pending"]):
            raise ValueError("Agree every pending worker")
        result = copy.deepcopy(selection)
        for identifier, assignment in assignments.items():
            if assignment["model"] not in selection["eligible_models"][identifier]:
                raise ValueError("Model is outside frozen catalog")
            result["assignments"][identifier] = {
                **assignment,
                "role": "developer",
                "source": "coordinator",
            }
        result.update(status="ready", pending=[])
        return result


class TeamRunTests(RpcHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.root / "peer.py").write_text(TEAM_PEER)
        self.runs = self.bridge.team_runs
        self.models = AgreedModels()
        self.runs.selection = self.models

    def step(
        self,
        name,
        *,
        depends=(),
        gate=False,
        owned=None,
        work=False,
        goal=None,
        outcome="success",
        source=None,
        read_reports=False,
    ):
        config = {
            "gate": gate,
            "outcome": outcome,
            "source": source,
            "read_reports": read_reports,
        }
        if owned:
            config["write"] = owned
        return {
            "id": name,
            "title": name,
            "depends_on": list(depends),
            "mode": "work" if work else "analyze",
            "contract": {
                "goal": goal or "team:" + name,
                "context": json.dumps(config),
                "scope": {"owned_files": [owned] if owned else []},
                "acceptance": ["Produce the requested answer"],
                "requirements": {"requires_write": bool(owned)},
            },
        }

    def plan(self, *steps, **options):
        return {
            "goal": "Integrate the team results",
            "steps": list(steps),
            "budget_seconds": 30,
            **options,
        }

    async def begin(self, plan, key=None):
        return await asyncio.to_thread(self.runs.start, key or str(uuid4()), plan)

    async def until(self, predicate):
        async def wait():
            while True:
                result = await asyncio.to_thread(predicate)
                if result:
                    return result
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(wait(), 8)

    async def finished(self, run_id):
        await self.until(lambda: self.runs.state(run_id) not in ACTIVE_TEAM)
        return await asyncio.to_thread(self.runs.view, run_id, True)

    async def entered(self, name):
        await self.until(lambda: (self.root / (name + ".entered")).exists())

    def release(self, name):
        (self.root / (name + ".release")).touch()

    @staticmethod
    def assignments(*identifiers):
        return {
            identifier: {
                "model": "openai-codex/baseline",
                "thinking": "high",
                "reason": "Agreed with OMP",
            }
            for identifier in identifiers
        }

    async def test_assigned_reviewers_run_while_integrator_model_is_pending(self):
        self.models.pending = {"final"}
        run = await self.begin(
            self.plan(
                self.step("left", gate=True),
                self.step("right", gate=True),
                self.step("final", depends=("left", "right")),
            )
        )
        await self.entered("left")
        await self.entered("right")
        observed = self.runs.observation(run["run_id"])
        self.assertEqual(observed["status"], "running")
        self.assertEqual(observed["pending_models"], ["final"])
        self.assertEqual(observed["next_action"], "agree_models")
        self.release("left")
        self.release("right")
        await self.until(lambda: self.runs.state(run["run_id"]) == "waiting_models")
        self.assertFalse((self.root / "final.entered").exists())
        await asyncio.to_thread(
            self.runs.assign, run["run_id"], self.assignments("final")
        )
        result = await self.finished(run["run_id"])
        self.assertEqual(result["answer"], "final:left,right")
        self.assertEqual(self.models.calls, 1)

    async def test_question_and_model_attention_coexist_and_assignment_keeps_question(
        self,
    ):
        self.models.pending = {"final"}
        run = await self.begin(
            self.plan(
                self.step("question", goal="question"),
                self.step("final", depends=("question",)),
            )
        )
        await self.until(
            lambda: bool(self.runs.observation(run["run_id"])["questions"])
        )
        observed = self.runs.observation(run["run_id"])
        view = self.runs.view(run["run_id"])
        self.assertEqual(observed["status"], "waiting_input")
        self.assertEqual(observed["pending_models"], ["final"])
        self.assertEqual(observed["next_action"], "reply")
        self.assertEqual(
            [item["action"] for item in view["actions"]], ["reply", "assign"]
        )
        self.assertNotIn("pending", view)
        await asyncio.to_thread(
            self.runs.assign, run["run_id"], self.assignments("final")
        )
        after = self.runs.observation(run["run_id"])
        self.assertEqual(after["questions"], observed["questions"])
        self.assertEqual(after["pending_models"], [])
        self.assertEqual(after["next_action"], "reply")
        self.assertFalse((self.root / "final.entered").exists())
        question = after["questions"][0]
        await asyncio.to_thread(
            self.bridge.reply, question["task_id"], question["question_id"], "blue"
        )
        self.assertEqual(
            (await self.finished(run["run_id"]))["answer"], "final:Selection recorded"
        )

    async def test_expired_questions_are_not_actions_even_before_native_settlement(
        self,
    ):
        self.models.pending = {"final"}
        run = await self.begin(
            self.plan(
                self.step("question", goal="question"),
                self.step("final", depends=("question",)),
            )
        )
        await self.until(
            lambda: bool(self.runs.observation(run["run_id"])["questions"])
        )
        with self.runs.guard, closing(self.bridge.tasks.connect()) as db:
            question = self.runs.observation(run["run_id"])["questions"][0]
            db.execute(
                "UPDATE questions SET deadline=0 WHERE question_id=?",
                (question["question_id"],),
            )
            observed = self.runs.observation(run["run_id"])
            self.assertEqual(observed["questions"], [])
            self.assertNotEqual(observed["next_action"], "reply")
            self.assertIsNone(self.runs.view(run["run_id"])["question"])
        await asyncio.to_thread(self.runs.cancel, run["run_id"])

    async def test_stale_assignment_cannot_revive_newly_blocked_dependency(self):
        self.models.pending = {"spare", "final"}
        run = await self.begin(
            self.plan(
                self.step("bad", gate=True, outcome="partial"),
                self.step("question", goal="question"),
                self.step("spare"),
                self.step("final", depends=("bad", "question", "spare")),
            )
        )
        await self.entered("bad")
        await self.until(
            lambda: bool(self.runs.observation(run["run_id"])["questions"])
        )
        original = self.runs.view(run["run_id"])["selection"]
        # Native settlement occurs while the controller is excluded. assign must
        # refresh dependency states itself, not trust its last displayed choices.
        with self.runs.guard:
            worker_id = self.runs.view(run["run_id"])["steps"][0]["task_id"]
            self.release("bad")
            await self.until(
                lambda: (
                    self.bridge.view(worker_id, refresh=False)["status"] == "completed"
                )
            )
            with self.assertRaises(ValueError):
                self.runs.assign(run["run_id"], self.assignments("spare", "final"))
            refreshed = self.runs.view(run["run_id"])
            self.assertEqual(refreshed["pending_models"], ["spare"])
            self.assertEqual(refreshed["steps"][-1]["status"], "blocked")
            self.runs.assign(run["run_id"], self.assignments("spare"))
            accepted = self.runs.view(run["run_id"])["selection"]
            self.assertEqual(accepted["jev"], original["jev"])
            self.assertEqual(accepted["pending_reasons"], original["pending_reasons"])
            self.assertEqual(
                accepted["assignments"]["question"], original["assignments"]["question"]
            )
        await self.entered("spare")
        await asyncio.to_thread(self.runs.cancel, run["run_id"])
        self.assertFalse((self.root / "final.entered").exists())

    async def test_readonly_synthesis_reads_genuine_partial_report_and_source(self):
        self.models.pending = {"final"}
        final = self.step("final", depends=("review",), read_reports=True)
        final.update(mode="think", dependency_policy="reported_evidence")
        run = await self.begin(
            self.plan(
                self.step(
                    "review",
                    outcome="partial",
                    source="Primary source: coverage excludes the private endpoint.",
                ),
                final,
            )
        )
        await self.until(lambda: self.runs.state(run["run_id"]) == "waiting_models")
        predecessor = self.runs.view(run["run_id"], True)["steps"][0]
        task = self.bridge.tasks.get(predecessor["task_id"])
        actual_id = predecessor["outcome_artifact_id"]
        self.assertIn(actual_id, json.loads(task["result_artifacts"]))
        # A newer unrecorded exact copy, a recorded same-owner wrong report,
        # and recorded foreign-owner exact reports cannot replace native evidence.
        unrecorded = self.bridge.artifacts.publish(
            task["conversation_id"],
            task["task_id"],
            "outcome",
            task["report_json"],
            "application/json",
        )["artifact_id"]
        decoys = [
            self.bridge.artifacts.publish(
                task["conversation_id"],
                task["task_id"],
                "outcome",
                "{}",
                "application/json",
            )["artifact_id"],
            self.bridge.artifacts.publish(
                task["conversation_id"],
                str(uuid4()),
                "outcome",
                task["report_json"],
                "application/json",
            )["artifact_id"],
            self.bridge.artifacts.publish(
                str(uuid4()),
                task["task_id"],
                "outcome",
                task["report_json"],
                "application/json",
            )["artifact_id"],
        ]
        self.bridge.tasks.update(
            task["task_id"],
            result_artifacts=json.dumps(json.loads(task["result_artifacts"]) + decoys),
        )
        await asyncio.to_thread(
            self.runs.assign, run["run_id"], self.assignments("final")
        )
        result = await self.finished(run["run_id"])
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "partial")
        )
        self.assertEqual(result["next_action"], "review_partial_result")
        self.assertEqual(result["incomplete_steps"], ["review"])
        recovered = json.loads(result["answer"])
        self.assertEqual(recovered["reports"][0]["outcome"], "partial")
        self.assertEqual(recovered["reports"][0]["checks"][0]["result"], "not_run")
        self.assertEqual(
            recovered["reports"][0]["blockers"], ["One source remains unavailable"]
        )
        self.assertEqual(
            recovered["sources"],
            ["Primary source: coverage excludes the private endpoint."],
        )
        evidence = result["steps"][-1]["dependency_evidence"][0]
        self.assertEqual(evidence["outcome_artifact_id"], actual_id)
        self.assertNotEqual(evidence["outcome_artifact_id"], unrecorded)
        self.assertEqual(evidence["outcome"], "partial")
        self.assertEqual(result["steps"][-1]["outcome"], "success")
        self.assertEqual(
            self.bridge.tasks.get(task["task_id"])["report_json"], task["report_json"]
        )

    async def test_partial_report_does_not_release_default_or_development_integrator(
        self,
    ):
        for work in (False, True):
            with self.subTest(work=work):
                final_name = "development" if work else "analysis"
                run = await self.begin(
                    self.plan(
                        self.step("review", outcome="partial"),
                        self.step(final_name, depends=("review",), work=work),
                        allow_work=work,
                    )
                )
                result = await self.finished(run["run_id"])
                self.assertEqual(
                    (result["status"], result["outcome"]), ("failed", "blocked")
                )
                self.assertEqual(result["steps"][0]["outcome"], "partial")
                self.assertIsNone(result["steps"][-1]["task_id"])
                self.assertFalse((self.root / (final_name + ".entered")).exists())

    async def test_evidence_policy_never_accepts_blocked_or_unreported_predecessor(
        self,
    ):
        for goal in ("blocked", "missing-report"):
            with self.subTest(goal=goal):
                final = self.step("final", depends=("review",))
                final.update(mode="think", dependency_policy="reported_evidence")
                result = await self.finished(
                    (
                        await self.begin(
                            self.plan(
                                self.step("review", goal=goal),
                                final,
                            )
                        )
                    )["run_id"]
                )
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["steps"][-1]["task_id"])
                self.assertFalse((self.root / "final.entered").exists())

    async def test_partial_final_is_not_a_successful_evidence_synthesis(self):
        final = self.step("final", depends=("review",), outcome="partial")
        final.update(mode="think", dependency_policy="reported_evidence")
        result = await self.finished(
            (
                await self.begin(
                    self.plan(
                        self.step("review", outcome="partial"),
                        final,
                    )
                )
            )["run_id"]
        )
        self.assertEqual((result["status"], result["outcome"]), ("failed", "blocked"))
        self.assertEqual(result["incomplete_steps"], ["review", "final"])
        self.assertEqual(result["steps"][-1]["outcome"], "partial")

    async def test_legacy_request_key_preserves_saved_bytes_and_rejects_policy_change(
        self,
    ):
        self.models.pending = True
        final = self.step("final", depends=("first",))
        final["mode"] = "think"
        plan = self.plan(self.step("first"), final)
        run = await self.begin(plan, "legacy")
        await self.until(lambda: self.runs.state(run["run_id"]) == "waiting_models")
        with closing(self.bridge.tasks.connect()) as db:
            stored = json.loads(
                db.execute(
                    "SELECT plan_json FROM team_runs WHERE run_id=?", (run["run_id"],)
                ).fetchone()["plan_json"]
            )
            for step in stored["steps"]:
                step.pop("dependency_policy")
            legacy = json.dumps(
                stored, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            db.execute(
                "UPDATE team_runs SET plan_json=? WHERE run_id=?",
                (legacy, run["run_id"]),
            )
        explicit = copy.deepcopy(plan)
        for step in explicit["steps"]:
            step["dependency_policy"] = "success"
        self.assertEqual(
            (await self.begin(explicit, "legacy"))["run_id"], run["run_id"]
        )
        explicit["steps"][-1]["dependency_policy"] = "reported_evidence"
        with self.assertRaises(ValueError):
            await self.begin(explicit, "legacy")
        with closing(self.bridge.tasks.connect()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT plan_json FROM team_runs WHERE run_id=?", (run["run_id"],)
                ).fetchone()["plan_json"],
                legacy,
            )
        self.assertEqual(self.models.calls, 1)
        self.assertFalse((self.root / "first.entered").exists())

    async def test_legacy_request_key_racing_preflight_is_not_reselected(self):
        self.models.pending = True
        plan = self.plan(self.step("first"), self.step("final", depends=("first",)))
        other = TeamRuns(
            self.bridge.tasks,
            self.bridge.scope,
            self.bridge.start,
            self.bridge.view,
            self.bridge.cancel,
            self.models,
            str(uuid4()),
        )
        original = self.runs.start_task
        admitted = []

        def race(**kwargs):
            if kwargs.get("dry_run") and not admitted:
                admitted.append(other.start("racing-key", plan)["run_id"])
                with closing(self.bridge.tasks.connect()) as db:
                    stored = json.loads(
                        db.execute(
                            "SELECT plan_json FROM team_runs WHERE run_id=?",
                            (admitted[0],),
                        ).fetchone()["plan_json"]
                    )
                    for step in stored["steps"]:
                        step.pop("dependency_policy")
                    db.execute(
                        "UPDATE team_runs SET plan_json=? WHERE run_id=?",
                        (
                            json.dumps(
                                stored,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            admitted[0],
                        ),
                    )
            return original(**kwargs)

        self.runs.start_task = race
        try:
            repeated = await self.begin(plan, "racing-key")
            self.assertEqual(repeated["run_id"], admitted[0])
            await self.until(lambda: other.state(admitted[0]) == "waiting_models")
            self.assertEqual(self.models.calls, 1)
            self.assertFalse((self.root / "first.entered").exists())
        finally:
            self.runs.start_task = original
            await asyncio.to_thread(other.close)

    async def test_dag_integrates_all_successful_predecessors_and_preserves_contract(
        self,
    ):
        plan = self.plan(
            self.step("left"),
            self.step("right"),
            self.step("final", depends=("left", "right")),
        )
        result = await self.finished((await self.begin(plan))["run_id"])
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "success")
        )
        self.assertEqual(result["answer"], "final:left,right")
        final = result["steps"][-1]
        self.assertEqual(
            final["result"]["contract"]["acceptance"], ["Produce the requested answer"]
        )
        self.assertEqual(
            {item["step_id"] for item in final["dependency_evidence"]},
            {"left", "right"},
        )
        self.assertTrue(
            all(item["answer_artifact_id"] for item in final["dependency_evidence"])
        )
        self.assertEqual(self.models.calls, 1)

    async def test_overlapping_writes_serialize_but_disjoint_writes_can_finish(self):
        plan = self.plan(
            self.step("first", gate=True, owned="shared.txt", work=True),
            self.step("second", owned="shared.txt", work=True),
            self.step("disjoint", owned="other.txt", work=True),
            self.step("final", depends=("first", "second", "disjoint")),
            allow_work=True,
        )
        run = await self.begin(plan)
        await self.entered("first")
        await self.entered("disjoint")
        self.assertFalse((self.root / "second.entered").exists())
        self.release("first")
        result = await self.finished(run["run_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / "shared.txt").read_text(), "first\nsecond\n")
        self.assertEqual((self.root / "other.txt").read_text(), "disjoint\n")

    async def test_unowned_work_is_exclusive_even_against_analysis(self):
        plan = self.plan(
            self.step("barrier", gate=True, work=True),
            self.step("analysis"),
            self.step("final", depends=("barrier", "analysis")),
            allow_work=True,
        )
        run = await self.begin(plan)
        await self.entered("barrier")
        await asyncio.sleep(0.12)
        self.assertFalse((self.root / "analysis.entered").exists())
        with self.assertRaises(TaskAdmissionBusy):
            await asyncio.to_thread(
                self.bridge.start, prompt="paragraph", mode="analyze"
            )
        self.release("barrier")
        self.assertEqual(
            (await self.finished(run["run_id"]))["answer"], "final:barrier,analysis"
        )

    async def test_cancellation_never_launches_pending_or_final_steps(self):
        self.models.pending = {"pending", "final"}
        run = await self.begin(
            self.plan(
                self.step("first", gate=True),
                self.step("pending"),
                self.step("final", depends=("first", "pending")),
                max_parallel=1,
            )
        )
        await self.entered("first")
        cancelled = await asyncio.to_thread(self.runs.cancel, run["run_id"])
        with self.assertRaises(ValueError):
            self.runs.assign(run["run_id"], self.assignments("pending", "final"))
        self.assertEqual(self.runs.observation(run["run_id"])["pending_models"], [])
        self.release("first")
        await asyncio.sleep(0.12)
        self.assertIn(cancelled["status"], ("cancelling", "cancelled"))
        self.assertFalse((self.root / "pending.entered").exists())
        self.assertFalse((self.root / "final.entered").exists())
        self.assertEqual((await self.finished(run["run_id"]))["status"], "cancelled")

    async def test_late_selection_cannot_revive_cancelled_team(self):
        self.models.release = threading.Event()
        run = await self.begin(
            self.plan(self.step("first"), self.step("final", depends=("first",)))
        )
        await self.until(self.models.entered.is_set)
        await asyncio.to_thread(self.runs.cancel, run["run_id"])
        self.models.release.set()
        await asyncio.sleep(0.12)
        self.assertEqual(self.runs.state(run["run_id"]), "cancelled")
        self.assertFalse((self.root / "first.entered").exists())

    async def test_blocked_or_unstructured_predecessor_blocks_dependents_not_other_outputs(
        self,
    ):
        for goal in ("blocked", "missing-report"):
            with self.subTest(goal=goal):
                run = await self.begin(
                    self.plan(
                        self.step("bad", goal=goal),
                        self.step("good"),
                        self.step("final", depends=("bad", "good")),
                    )
                )
                result = await self.finished(run["run_id"])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["steps"][-1]["status"], "blocked")
                self.assertIsNone(result["steps"][-1]["task_id"])
                self.assertEqual(result["steps"][1]["result"]["answer"], "good:")
                self.assertIsNone(result["answer"])

    async def test_waiting_models_requires_complete_agreement_before_any_launch(self):
        self.models.pending = True
        run = await self.begin(
            self.plan(self.step("first"), self.step("final", depends=("first",)))
        )
        await self.until(lambda: self.runs.state(run["run_id"]) == "waiting_models")
        view = self.runs.view(run["run_id"])
        self.assertEqual(view["next_action"], "agree_models")
        self.assertFalse((self.root / "first.entered").exists())
        with self.assertRaises(ValueError):
            self.runs.assign(
                run["run_id"],
                {
                    "first": {
                        "model": "openai-codex/baseline",
                        "thinking": "high",
                        "reason": "Agreed",
                    }
                },
            )
        await asyncio.to_thread(
            self.runs.assign,
            run["run_id"],
            {
                key: {
                    "model": "openai-codex/baseline",
                    "thinking": "high",
                    "reason": "Agreed with OMP",
                }
                for key in view["pending_models"]
            },
        )
        self.assertEqual((await self.finished(run["run_id"]))["answer"], "final:first")
        self.assertEqual(self.models.calls, 1)

    async def test_project_scoped_idempotency_and_live_foreign_owner_refuses_control(
        self,
    ):
        self.models.pending = True
        plan = self.plan(self.step("first"), self.step("final", depends=("first",)))
        run = await self.begin(plan, "same-key")
        other = TeamRuns(
            self.bridge.tasks,
            self.bridge.scope,
            self.bridge.start,
            self.bridge.view,
            self.bridge.cancel,
            self.models,
            str(uuid4()),
        )
        try:
            repeated = await asyncio.to_thread(other.start, "same-key", plan)
            self.assertEqual(repeated["run_id"], run["run_id"])
            with self.assertRaises(ValueError):
                other.cancel(run["run_id"])
            altered = copy.deepcopy(plan)
            altered["goal"] = "Different work"
            with self.assertRaises(ValueError):
                other.start("same-key", altered)
        finally:
            await asyncio.to_thread(other.close)

    async def test_dead_owner_reserved_work_is_reported_and_never_replayed(self):
        self.models.pending = True
        plan = self.plan(self.step("first"), self.step("final", depends=("first",)))
        run = await self.begin(plan, "lost-owner")
        await self.until(lambda: self.runs.state(run["run_id"]) == "waiting_models")
        # Simulate a durable crash checkpoint without an admitted native task.
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE team_runs SET owner=?,status=? WHERE run_id=?",
                (str(uuid4()), "running", run["run_id"]),
            )
            db.execute(
                "UPDATE team_steps SET status='reserved',task_id=? WHERE run_id=? AND step_id='first'",
                (str(uuid4()), run["run_id"]),
            )
        result = self.runs.view(run["run_id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["steps"][0]["status"], "interrupted")
        retry = await self.begin(plan, "lost-owner")
        self.assertEqual(retry["run_id"], run["run_id"])
        self.assertEqual(retry["status"], "interrupted")
        self.assertFalse((self.root / "first.entered").exists())
        self.assertEqual(self.models.calls, 1)

    async def test_capacity_busy_queues_but_uncertain_admission_never_replays(self):
        original = self.runs.start_task
        attempts = []
        available = threading.Event()

        def busy(**kwargs):
            if not kwargs.get("dry_run"):
                attempts.append(kwargs["reserved_task_id"])
                if not available.is_set():
                    raise TaskAdmissionBusy("All worker slots occupied")
            return original(**kwargs)

        self.runs.start_task = busy
        run = await self.begin(
            self.plan(self.step("first"), self.step("final", depends=("first",)))
        )
        await self.until(lambda: bool(attempts))
        self.assertFalse((self.root / "first.entered").exists())
        available.set()
        self.assertEqual((await self.finished(run["run_id"]))["status"], "completed")

        failures = []

        def uncertain(**kwargs):
            if not kwargs.get("dry_run"):
                failures.append(kwargs["reserved_task_id"])
                raise RuntimeError("Connection lost after dispatch")
            return original(**kwargs)

        self.runs.start_task = uncertain
        failed = await self.begin(
            self.plan(self.step("never"), self.step("end", depends=("never",)))
        )
        self.assertEqual(
            (await self.finished(failed["run_id"]))["status"], "interrupted"
        )
        await asyncio.sleep(0.12)
        self.assertEqual(len(failures), 1)
        self.assertFalse((self.root / "end.entered").exists())

    async def test_invalid_later_contract_rejects_whole_team_before_work(self):
        invalid = self.step("final", depends=("first",))
        invalid["contract"]["artifact_ids"] = [str(uuid4())]
        with self.assertRaises(ValueError):
            await self.begin(self.plan(self.step("first"), invalid))
        self.assertEqual(self.models.calls, 0)
        self.assertFalse((self.root / "first.entered").exists())
        with self.assertRaises(ValueError):
            await self.begin(
                self.plan(
                    self.step("first"),
                    self.step("final", depends=("first",)),
                    cwd=str(self.root.parent),
                )
            )

    async def test_worker_question_can_be_answered_without_releasing_dependencies_early(
        self,
    ):
        run = await self.begin(
            self.plan(
                self.step("question", goal="question"),
                self.step("final", depends=("question",)),
            )
        )
        await self.until(lambda: self.runs.state(run["run_id"]) == "waiting_input")
        waiting = self.runs.view(run["run_id"])
        self.assertEqual(waiting["next_action"], "reply")
        self.assertFalse((self.root / "final.entered").exists())
        await asyncio.to_thread(
            self.bridge.reply,
            waiting["task_id"],
            waiting["question"]["question_id"],
            "blue",
        )
        result = await self.finished(run["run_id"])
        self.assertEqual(result["steps"][0]["result"]["answer"], "blue")
        self.assertEqual(result["answer"], "final:Selection recorded")

    async def test_model_agreement_after_deadline_cannot_launch_work(self):
        self.models.pending = True
        run = await self.begin(
            self.plan(self.step("first"), self.step("final", depends=("first",)))
        )
        await self.until(lambda: self.runs.state(run["run_id"]) == "waiting_models")
        with self.runs.guard:
            self.runs.monotonic_deadlines[run["run_id"]] = 0
            result = self.runs.assign(
                run["run_id"],
                {
                    key: {
                        "model": "openai-codex/baseline",
                        "thinking": "high",
                        "reason": "Too late",
                    }
                    for key in ("first", "final")
                },
            )
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / "first.entered").exists())

    async def test_cancellation_does_not_settle_while_native_worker_is_active(self):
        run = await self.begin(
            self.plan(
                self.step("question", goal="question"),
                self.step("final", depends=("question",)),
            )
        )
        await self.until(lambda: self.runs.state(run["run_id"]) == "waiting_input")
        original = self.runs.cancel_task
        deliver = threading.Event()

        def delayed_cancel(task_id):
            if deliver.is_set():
                return original(task_id)
            return self.bridge.view(task_id, refresh=False)

        self.runs.cancel_task = delayed_cancel
        try:
            pending = await asyncio.to_thread(self.runs.cancel, run["run_id"])
            self.assertEqual(pending["status"], "cancelling")
            self.assertEqual(pending["next_action"], "wait")
            self.assertEqual(self.runs.observation(run["run_id"])["questions"], [])
            self.assertEqual(pending["actions"], [])
            worker_id = pending["steps"][0]["task_id"]
            self.assertEqual(
                self.bridge.view(worker_id, refresh=False)["status"], "waiting_input"
            )
            self.assertFalse((self.root / "final.entered").exists())
            deliver.set()
            settled = await self.finished(run["run_id"])
            self.assertEqual(settled["status"], "cancelled")
            self.assertEqual(
                self.bridge.view(worker_id, refresh=False)["status"], "cancelled"
            )
        finally:
            deliver.set()
            self.runs.cancel_task = original
