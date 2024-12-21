"""
This module provides simpler types to use with the server for managing prompts
and tools.
"""

from pydantic import BaseModel

from mcp_wcgw.types import (
    ServerCapabilities,
)


class InitializationOptions(BaseModel):
    server_name: str
    server_version: str
    capabilities: ServerCapabilities
