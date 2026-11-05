"""
ClaudeCodeModel — agency-swarm Model whose LLM is a Python async callback.
Copied from scratchpad; brain_fn IS Claude Code reasoning in-session.
"""
import json
import uuid
from typing import Callable, Awaitable, AsyncIterator

from agents import Usage
from agents.models.interface import Model, ModelResponse
from agents.model_settings import ModelSettings
from agents.handoffs import Handoff
from agents.tool import Tool
from agents import ModelTracing, AgentOutputSchemaBase
from openai.types.shared_params import ResponseFormatText
from openai.types.responses import (
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseFunctionToolCall,
)

BrainFn = Callable[[str | None, list, list[dict]], Awaitable[tuple[str | None, list[dict]]]]


class ClaudeCodeModel(Model):
    def __init__(self, brain_fn: BrainFn, model_name: str = "claude-code-in-session"):
        self.brain_fn   = brain_fn
        self.model_name = model_name

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list,
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt=None,
    ) -> ModelResponse:

        tool_schemas = []
        for t in tools:
            if hasattr(t, "params_json_schema"):
                tool_schemas.append({
                    "name": t.name,
                    "description": getattr(t, "description", ""),
                    "parameters": t.params_json_schema,
                })
            elif hasattr(t, "name"):
                tool_schemas.append({"name": t.name, "description": str(t)})

        messages = input if isinstance(input, list) else [{"role": "user", "content": input}]
        text, tool_calls = await self.brain_fn(system_instructions, messages, tool_schemas)

        output: list = []
        for tc in tool_calls:
            cid = f"call_{uuid.uuid4().hex[:12]}"
            output.append(ResponseFunctionToolCall(
                id=cid, call_id=cid,
                name=tc["name"],
                arguments=json.dumps(tc.get("arguments", {})),
                type="function_call",
            ))
        if text:
            output.append(ResponseOutputMessage(
                id=f"msg_{uuid.uuid4().hex[:12]}",
                content=[ResponseOutputText(text=text, type="output_text", annotations=[])],
                role="assistant", status="completed", type="message",
            ))

        return ModelResponse(
            output=output,
            usage=Usage(requests=1, input_tokens=0, output_tokens=0, total_tokens=0),
            response_id=None,
        )

    async def stream_response(self, *args, **kwargs) -> AsyncIterator:
        from agents.models.fake_id import FAKE_RESPONSES_ID
        response = await self.get_response(*args, **kwargs)
        from openai.types.responses.response_completed_event import ResponseCompletedEvent
        from openai.types.responses import Response

        async def _gen():
            yield ResponseCompletedEvent(
                type="response.completed",
                response=Response(
                    id=response.response_id or FAKE_RESPONSES_ID,
                    created_at=0, error=None, incomplete_details=None,
                    instructions=None, metadata={},
                    model=self.model_name,
                    object="realtime.response",
                    output=response.output,
                    parallel_tool_calls=False,
                    temperature=None, tool_choice="auto",
                    tools=[], top_p=None, max_output_tokens=None,
                    usage=None, status="completed", voice=None,
                )
            )
        return _gen()
