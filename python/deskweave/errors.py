from __future__ import annotations


class DeskWeaveError(Exception):
    def __init__(
        self, code: str, message: str, *, retry: str = "never", details: dict | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry = retry
        self.details = details

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message, "retry": self.retry}
        if self.details is not None:
            result["details"] = self.details
        return result
