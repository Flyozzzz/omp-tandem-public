"""The returned decision launch descriptor must work through the real MCP/RPC boundary."""

from tests.helpers import PEER, RpcHarness, patch_peer

DECISION_START = r"""scenario = task['task']['goal']
        if task['task'].get('artifact_ids'):
            packet_id = task['task']['artifact_ids'][0]
            packet_parts = []
            emit({'type':'host_tool_call','id':'decision-packet','toolCallId':'decision-packet','toolName':'tandem_read_artifact','arguments':{'artifact_id':packet_id}})
            continue"""

DECISION_READS = r"""    elif kind == 'host_tool_result' and command['id'] == 'decision-packet':
        page = json.loads(command['result']['content'][0]['text'])
        packet_parts.append(page['content'])
        if page['next_offset'] is not None:
            emit({'type':'host_tool_call','id':'decision-packet','toolCallId':'decision-packet','toolName':'tandem_read_artifact','arguments':{'artifact_id':packet_id,'offset':page['next_offset']}})
        else:
            packet = json.loads(''.join(packet_parts))
            brief_parts = []
            emit({'type':'host_tool_call','id':'decision-brief','toolCallId':'decision-brief','toolName':'tandem_read_artifact','arguments':{'artifact_id':packet['brief_artifact_id']}})
    elif kind == 'host_tool_result' and command['id'] == 'decision-brief':
        page = json.loads(command['result']['content'][0]['text'])
        brief_parts.append(page['content'])
        if page['next_offset'] is not None:
            emit({'type':'host_tool_call','id':'decision-brief','toolCallId':'decision-brief','toolName':'tandem_read_artifact','arguments':{'artifact_id':packet['brief_artifact_id'],'offset':page['next_offset']}})
        else:
            brief = ''.join(brief_parts)
            assessment = {'preparation_id':packet['preparation_id'],'verdict':'derivable','answer':'blue','rationale':'The pinned brief states the value.','reversible':True,'in_scope':True,'risks':[], 'citations':[{'source_sha256':packet['source_sha256'],'start_bytes':0,'end_bytes':len(brief.encode()),'quote':brief}]}
            finish({'outcome':'success','summary':'Pinned-source assessment','answer':json.dumps(assessment)})
    elif kind == 'host_tool_result' and command['id'] == 'question':"""


class DecisionIntegrationTests(RpcHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        source = patch_peer(PEER, "scenario = task['task']['goal']", DECISION_START)
        source = patch_peer(
            source,
            "    elif kind == 'host_tool_result' and command['id'] == 'question':",
            DECISION_READS,
        )
        (self.root / "peer.py").write_text(source)

    async def test_prepared_consultation_reads_pin_and_replies_with_reserved_capacity(
        self,
    ):
        waiting = []
        for _ in range(3):
            task = await self.start(timeout_seconds=60)
            state = await self.result(task["task_id"])
            self.assertEqual(state["status"], "waiting_input")
            waiting.append(state)
        first = waiting[0]
        brief_text = "Choose blue for the task-local reversible value."
        brief = await self.call(
            "tandem_publish_artifact",
            conversation_id=first["conversation_id"],
            name="brief",
            content=brief_text,
        )
        policy = await self.call(
            "tandem_decision",
            request={
                "action": "policy",
                "task_id": first["task_id"],
                "policy": {
                    "source": {"kind": "artifact", "artifact_id": brief["artifact_id"]},
                    "reversible": True,
                    "in_scope": True,
                },
            },
        )
        prepared = await self.call(
            "tandem_decision",
            request={
                "action": "prepare",
                "task_id": first["task_id"],
                "question_id": first["question"]["question_id"],
                "policy_id": policy["policy_id"],
            },
        )
        launch = prepared["consultation"]
        consultation = await self.call(launch["tool"], **launch["arguments"])
        result = await self.result(consultation["task_id"])
        self.assertEqual(result["status"], "completed", result)
        decision = await self.call(
            "tandem_decision",
            request={
                "action": "record",
                "preparation_id": prepared["preparation_id"],
                "consultation_task_id": consultation["task_id"],
            },
        )
        basis = await self.read_section(decision["reader"])
        self.assertIsNone(basis["reply_json"])
        self.assertEqual(
            basis["basis"]["assessment"]["citations"][0]["quote"], brief_text
        )
        reply_request = {"action": "reply", "decision_id": decision["decision_id"]}
        reply = await self.call("tandem_decision", request=reply_request)
        self.assertTrue(reply["accepted"])
        self.assertEqual(
            await self.call("tandem_decision", request=reply_request), reply
        )
        final = await self.result(first["task_id"])
        self.assertEqual((final["status"], final["answer"]), ("completed", "blue"))
        overview = await self.call("tandem_overview")
        self.assertEqual(
            {
                item["id"]
                for item in overview["items"]
                if item["category"] == "pending_question"
            },
            {item["task_id"] for item in waiting[1:]},
        )
