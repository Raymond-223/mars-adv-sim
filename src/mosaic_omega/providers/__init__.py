"""External-provider transport helpers.

The package deliberately separates network transport from Agent semantics so a
run can prove whether a model request actually crossed an HTTP API boundary.
"""

from .openai_compatible import StdlibOpenAICompatibleClient, create_openai_compatible_client

__all__ = ["StdlibOpenAICompatibleClient", "create_openai_compatible_client"]
