class TransientError(RuntimeError):
    """일시적 장애. 큐에서 지연 재시도한다 (모델 API 시간 초과, MCP 연결 실패 등)."""


class InputError(ValueError):
    """입력 문제. 재시도하지 않고 사용자에게 수정을 요청한다."""


class ToolCallError(RuntimeError):
    """도구가 오류 결과를 돌려줬다. 조사 단계에서는 모델에게 그대로 전달한다."""
