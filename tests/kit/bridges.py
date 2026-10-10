"""Stand-in bridges for sidecar server tests: a fixed status, or recorded calls."""


class StubBridge:
    """Answers every call with a connected test bridge status."""

    async def call(self, method: str, params: dict):
        return {
            "connected": True,
            "nx_version": "NX test",
            "bridge_protocol": 1,
            "active_part": None,
        }


class RecordingBridge:
    """Records each ``(method, params)`` and answers with ``response``."""

    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict):
        self.calls.append((method, params))
        return self.response
