import anyio
import pytest

from mcp_wcgw.client.session import ClientSession
from mcp_wcgw.server import NotificationOptions, Server
from mcp_wcgw.server.models import InitializationOptions
from mcp_wcgw.server.session import ServerSession
from mcp_wcgw.types import (
    ClientNotification,
    InitializedNotification,
    JSONRPCMessage,
    PromptsCapability,
    ResourcesCapability,
    ServerCapabilities,
)


@pytest.mark.anyio
async def test_server_session_initialize():
    server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream(
        1, JSONRPCMessage
    )
    client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream(
        1, JSONRPCMessage
    )

    async def run_client(client: ClientSession):
        async for message in client_session.incoming_messages:
            if isinstance(message, Exception):
                raise message

    received_initialized = False

    async def run_server():
        nonlocal received_initialized

        async with ServerSession(
            client_to_server_receive,
            server_to_client_send,
            InitializationOptions(
                server_name="mcp",
                server_version="0.1.0",
                capabilities=ServerCapabilities(),
            ),
        ) as server_session:
            async for message in server_session.incoming_messages:
                if isinstance(message, Exception):
                    raise message

                if isinstance(message, ClientNotification) and isinstance(
                    message.root, InitializedNotification
                ):
                    received_initialized = True
                    return

    try:
        async with (
            ClientSession(
                server_to_client_receive, client_to_server_send
            ) as client_session,
            anyio.create_task_group() as tg,
        ):
            tg.start_soon(run_client, client_session)
            tg.start_soon(run_server)

            await client_session.initialize()
    except anyio.ClosedResourceError:
        pass

    assert received_initialized


@pytest.mark.anyio
async def test_server_capabilities():
    server = Server("test")
    notification_options = NotificationOptions()
    experimental_capabilities = {}

    # Initially no capabilities
    caps = server.get_capabilities(notification_options, experimental_capabilities)
    assert caps.prompts is None
    assert caps.resources is None

    # Add a prompts handler
    @server.list_prompts()
    async def list_prompts():
        return []

    caps = server.get_capabilities(notification_options, experimental_capabilities)
    assert caps.prompts == PromptsCapability(listChanged=False)
    assert caps.resources is None

    # Add a resources handler
    @server.list_resources()
    async def list_resources():
        return []

    caps = server.get_capabilities(notification_options, experimental_capabilities)
    assert caps.prompts == PromptsCapability(listChanged=False)
    assert caps.resources == ResourcesCapability(subscribe=False, listChanged=False)
