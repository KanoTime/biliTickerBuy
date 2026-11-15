from requests import RequestException


class BiliOrderResultUnknownError(RequestException):
    """An order may have been created; callers must reconcile before retrying."""

    def __init__(self, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause
