class AnalyticsError(Exception):
    """Only deliberate, non-sensitive messages may cross the API boundary."""

    def __init__(self, code: str, message: str, status: int = 422):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)
