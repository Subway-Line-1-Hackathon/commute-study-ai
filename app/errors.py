# code: (HTTP 상태, 사용자에게 보여줄 문장)
ERRORS = {
    "INVALID_REQUEST": (400, "요청 형식이 올바르지 않아요."),
    "UNAUTHORIZED": (401, "내부 키가 올바르지 않아요."),
    "UNSUPPORTED_FILE": (422, "열 수 없는 파일이에요. PDF, PPTX, PNG 파일로 다시 올려주세요."),
    "PAGE_LIMIT_EXCEEDED": (422, "쪽수가 너무 많아요. PDF·PPT는 100쪽, PNG는 20장까지 올릴 수 있어요."),
    "TEXT_EXTRACTION_FAILED": (422, "글자를 인식하지 못했어요. 글자가 선명한 파일로 다시 올려주세요."),
    "FILE_FETCH_FAILED": (502, "파일을 불러오지 못했어요. 잠시 후 다시 시도해 주세요."),
    "LLM_FAILED": (502, "AI 응답에 문제가 생겼어요. 잠시 후 다시 시도해 주세요."),
    "LLM_TIMEOUT": (504, "AI 응답이 늦어지고 있어요. 잠시 후 다시 시도해 주세요."),
}


class AIError(Exception):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(code)
        self.code = code
        self.status, default = ERRORS[code]
        self.message = message or default

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
