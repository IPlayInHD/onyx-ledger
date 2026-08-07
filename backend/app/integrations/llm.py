"""LLM adapters behind the LlmClient port.

`TemplateLlmClient` is a deterministic, offline explainer used in dev/tests: it
grounds its answer in the retrieved rule context and NEVER emits a dollar figure
(so it can't fabricate numbers — the guardrail then trivially holds). A real
provider adapter (Anthropic, etc.) drops in behind the same port; the AI service
and its guardrail are provider-agnostic.
"""
from __future__ import annotations

import re

from app.core.config import get_settings


class TemplateLlmClient:
    model = "template-explainer-1"

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        # Echo the retrieved rule context so the answer is demonstrably grounded.
        rules_section = ""
        m = re.search(r"Relevant tax rules:\n(.+?)(?:\n\nUser question:|\Z)", prompt, re.S)
        if m:
            rules_section = " ".join(line.strip("- ").strip()
                                     for line in m.group(1).splitlines() if line.strip())
        question = ""
        q = re.search(r"User question:\s*(.+)\Z", prompt, re.S)
        if q:
            question = q.group(1).strip()
        grounded = rules_section[:400] if rules_section else "the applicable Canadian tax rules"
        return (
            f"Regarding your question — “{question}” — here is an explanation grounded in "
            f"{grounded} This is educational, not filing advice; see the cited rules for detail."
        )


# Production adapter (behind the same port):
# class AnthropicLlmClient:
#     def __init__(self, client, model): self.client, self.model = client, model
#     async def complete(self, system, prompt, *, max_tokens=1024):
#         msg = await self.client.messages.create(
#             model=self.model, system=system, max_tokens=max_tokens,
#             messages=[{"role": "user", "content": prompt}])
#         return "".join(b.text for b in msg.content if b.type == "text")


def get_llm_client() -> TemplateLlmClient:
    _ = get_settings()  # provider/model selection happens here in production
    return TemplateLlmClient()
