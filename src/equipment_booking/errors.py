"""预约服务向 API 和 CLI 暴露的稳定错误。"""


class BookingError(RuntimeError):
    code = "booking_error"
    status = 400


class Unauthenticated(BookingError):
    code = "unauthenticated"
    status = 401


class NotFound(BookingError):
    code = "not_found"
    status = 404


class Conflict(BookingError):
    code = "conflict"
    status = 409


class Forbidden(BookingError):
    code = "forbidden"
    status = 403


class InvalidState(BookingError):
    code = "invalid_state"
    status = 409


class ValidationFailed(BookingError):
    code = "validation_failed"
    status = 422
