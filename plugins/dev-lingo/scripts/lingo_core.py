"""Shared coaching contract and display text, independent of the host CLI."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_INPUT = 64 * 1024
MAX_PROMPT = 16000
MAX_SENTENCES = 256


def coach_path():
    override = os.environ.get("DEV_LINGO_COACH_FILE")
    return Path(override).expanduser().resolve() if override else ROOT / "prompts/coach.txt"


def translation_environment():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("OMX_", "CODEX_THREAD", "CODEX_SESSION", "CODEX_PARENT", "CODEX_INTERNAL", "CLAUDE_PLUGIN", "CLAUDE_CODE", "CLAUDE_SESSION", "CLAUDE_PARENT", "PLUGIN_"))}
    env["DEV_LINGO_TRANSLATOR"] = "1"
    return env


def validate_result(value):
    if not isinstance(value, dict) or set(value) != {"explanation_label", "sentences"}:
        raise ValueError("올바른 영어 학습 결과를 받지 못했습니다.")
    label = value["explanation_label"]
    if not isinstance(label, str) or len(label) > 48 or "\n" in label or "\r" in label:
        raise ValueError("해설 표시 이름의 형식이 잘못되었습니다.")
    label = label.strip().rstrip(":：").strip()
    if not label:
        raise ValueError("해설 표시 이름이 비어 있습니다.")
    sentences = value["sentences"]
    if not isinstance(sentences, list) or not 1 <= len(sentences) <= MAX_SENTENCES:
        raise ValueError("통역 문장 목록이 비어 있거나 너무 깁니다.")
    normalized = []
    total_length = 0
    for sentence in sentences:
        if not isinstance(sentence, dict) or set(sentence) != {"english", "explanation"}:
            raise ValueError("통역 문장과 해설이 올바르게 연결되지 않았습니다.")
        english, explanation = sentence["english"], sentence["explanation"]
        if not isinstance(english, str) or not english.strip():
            raise ValueError("영어 문장이 비어 있거나 형식이 잘못되었습니다.")
        total_length += len(english)
        if total_length > 64000:
            raise ValueError("영어 결과가 너무 깁니다.")
        if not isinstance(explanation, str) or len(explanation) > 500:
            raise ValueError("해설 형식이 잘못되었거나 너무 깁니다.")
        normalized.append({"english": english.strip(), "explanation": " ".join(explanation.split())})
    return {"explanation_label": label, "sentences": normalized}


def format_translation(result):
    lines = []
    for sentence in result["sentences"]:
        lines.append("통역: " + sentence["english"])
        if sentence["explanation"]:
            lines.append(result["explanation_label"] + ": " + sentence["explanation"])
    return "\n".join(lines)
