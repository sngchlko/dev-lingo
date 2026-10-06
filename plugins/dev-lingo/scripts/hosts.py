"""Host-specific hook protocols and translation providers. Codex is active today."""
import codex_provider
from lingo_core import MAX_PROMPT, format_translation


class CodexHost:
    provider = codex_provider

    @staticmethod
    def prompt_from_event(payload):
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
            return None
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
            return None
        return prompt

    @staticmethod
    def notification(result):
        # Codex interprets this field as a UI warning, without adding model context.
        return {"systemMessage": format_translation(result)}


def get_host(name):
    if name == "codex":
        return CodexHost
    if name == "claude":
        raise NotImplementedError("Claude 지원은 아직 연결되지 않았습니다. 향후 Claude Code 번역 제공자가 필요합니다.")
    raise ValueError("지원하지 않는 호스트입니다: " + str(name))
