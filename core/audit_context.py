from contextvars import ContextVar

_metadata = ContextVar("audit_request_metadata", default={})

def set_audit_context(value): return _metadata.set(value)
def reset_audit_context(token): _metadata.reset(token)
def get_audit_context(): return _metadata.get()
