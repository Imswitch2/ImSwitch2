class CGHComputationError(RuntimeError):
    """Raised when a CGH algorithm cannot produce a result."""

    def __init__(self,message: str,details: str=None):
        super().__init__(message)
        self.details = details


class InvalidCGHResultError(ValueError):
    """Raised when an algorithm returns an invalid CGH field."""