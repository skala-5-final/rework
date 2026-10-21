"""컨텍스트 토큰 예산. 반복 횟수 한도(config의 *_max_*)와 별개로, 모델에 넣는 양을 제한한다.

1. 도구 결과 하나가 tool_result_max_chars를 넘으면 앞부분만 대화에 넣는다 (원본은 tool_results에 남아 판정·검사에 쓴다).
2. 모델 호출 전에 토큰을 세어 context_token_budget을 넘으면, 오래된 도구 결과부터 요약 표시로 바꾼다.
3. 그래도 넘으면 ContextBudgetExceeded. 조사 단계는 이를 받아 지금까지의 결과로 판정으로 넘어간다.
기본값은 시연 데이터 사용량(호출당 수천 토큰)보다 훨씬 넉넉하게 잡았다. 정상 실행에서는 걸리지 않는 안전망이다.
"""

import json
import logging
from functools import lru_cache

from langchain_core.messages import BaseMessage, ToolMessage

from rework.agent.errors import InputError

log = logging.getLogger("rework.budget")


class ContextBudgetExceeded(InputError):
    """정리해도 예산 안에 들어오지 않는다."""


@lru_cache
def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except Exception as e:  # noqa: BLE001 - 인코딩 파일을 못 받으면 길이로 어림한다
        log.warning("토큰 계산기를 쓸 수 없어 글자 수로 어림합니다: %s", e)
        return None


def count_text(text: str) -> int:
    enc = _encoder()
    # 한국어는 대략 글자당 1토큰 안팎. 넉넉하게(많게) 어림해 예산을 늦게 넘지 않게 한다.
    return len(enc.encode(text, disallowed_special=())) if enc else len(text)


def count_tokens(messages: list[BaseMessage]) -> int:
    total = 0
    for m in messages:
        c = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
        total += count_text(c) + 4  # 메시지마다 붙는 역할 표시
        if getattr(m, "tool_calls", None):
            total += count_text(json.dumps(m.tool_calls, ensure_ascii=False))
    return total


def clip(text: str, max_chars: int) -> tuple[str, bool]:
    """도구 결과 JSON이 너무 길면 앞부분만 남긴다. 잘렸는지 함께 돌려준다."""
    if len(text) <= max_chars:
        return text, False
    try:
        rid = json.loads(text).get("result_id")
    except (ValueError, AttributeError):
        rid = None
    return json.dumps({"result_id": rid, "truncated": True, "original_chars": len(text),
                       "preview": text[:max_chars]}, ensure_ascii=False), True


def fit(messages: list[BaseMessage], budget: int, keep_last: int = 2) -> tuple[list[BaseMessage], int, int]:
    """예산을 넘으면 오래된 도구 결과부터 생략 표시로 바꾼다. (정리한 메시지, 정리 전 토큰, 정리 후 토큰)."""
    before = count_tokens(messages)
    if before <= budget:
        return messages, before, before
    msgs = list(messages)
    tool_idx = [i for i, m in enumerate(msgs) if isinstance(m, ToolMessage)][:-keep_last or None]
    now = before
    for i in tool_idx:
        old = msgs[i]
        try:
            rid = json.loads(old.content).get("result_id")
        except (ValueError, AttributeError, TypeError):
            rid = None
        stub = json.dumps({"result_id": rid, "omitted": "컨텍스트 예산으로 생략. 필요하면 같은 도구를 다시 부르지 말고 지금까지의 요약으로 판단"},
                          ensure_ascii=False)
        msgs[i] = old.model_copy(update={"content": stub})
        now = count_tokens(msgs)
        if now <= budget:
            break
    return msgs, before, now
