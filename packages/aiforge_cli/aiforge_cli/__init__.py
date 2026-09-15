"""AIForge's terminal client.

A thin front-end: it starts the sandbox, then speaks HTTP + SSE to the API
already running inside it. No agent logic lives here — every decision about
what to do with a message is the sandbox's.
"""

__version__ = "0.1.0"
