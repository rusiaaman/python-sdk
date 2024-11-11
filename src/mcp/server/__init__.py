import contextvars
import logging
import warnings
from collections.abc import Awaitable, Callable
from typing import Any, Sequence

from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import AnyUrl

from mcp.server import types
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server as stdio_server
from mcp.shared.context import RequestContext
from mcp.shared.session import RequestResponder
from mcp.types import (
    METHOD_NOT_FOUND,
    CallToolRequest,
    ClientNotification,
    ClientRequest,
    CompleteRequest,
    EmbeddedResource,
    EmptyResult,
    ErrorData,
    JSONRPCMessage,
    ListPromptsRequest,
    ListPromptsResult,
    ListResourcesRequest,
    ListResourcesResult,
    ListToolsRequest,
    ListToolsResult,
    LoggingCapability,
    LoggingLevel,
    PingRequest,
    ProgressNotification,
    Prompt,
    PromptMessage,
    PromptReference,
    PromptsCapability,
    ReadResourceRequest,
    ReadResourceResult,
    Resource,
    ResourceReference,
    ResourcesCapability,
    ServerCapabilities,
    ServerResult,
    SetLevelRequest,
    SubscribeRequest,
    TextContent,
    Tool,
    ToolsCapability,
    UnsubscribeRequest,
)

logger = logging.getLogger(__name__)

request_ctx: contextvars.ContextVar[RequestContext] = contextvars.ContextVar(
    "request_ctx"
)


class NotificationOptions:
    def __init__(
        self,
        prompts_changed: bool = False,
        resources_changed: bool = False,
        tools_changed: bool = False,
    ):
        self.prompts_changed = prompts_changed
        self.resources_changed = resources_changed
        self.tools_changed = tools_changed


class Server:
    def __init__(self, name: str):
        self.name = name
        self.request_handlers: dict[type, Callable[..., Awaitable[ServerResult]]] = {
            PingRequest: _ping_handler,
        }
        self.notification_handlers: dict[type, Callable[..., Awaitable[None]]] = {}
        self.notification_options = NotificationOptions()
        logger.debug(f"Initializing server '{name}'")

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> types.InitializationOptions:
        """Create initialization options from this server instance."""

        def pkg_version(package: str) -> str:
            try:
                from importlib.metadata import version

                v = version(package)
                if v is not None:
                    return v
            except Exception:
                pass

            return "unknown"

        return types.InitializationOptions(
            server_name=self.name,
            server_version=pkg_version("mcp"),
            capabilities=self.get_capabilities(
                notification_options or NotificationOptions(),
                experimental_capabilities or {},
            ),
        )

    def get_capabilities(
        self,
        notification_options: NotificationOptions,
        experimental_capabilities: dict[str, dict[str, Any]],
    ) -> ServerCapabilities:
        """Convert existing handlers to a ServerCapabilities object."""
        prompts_capability = None
        resources_capability = None
        tools_capability = None
        logging_capability = None

        # Set prompt capabilities if handler exists
        if ListPromptsRequest in self.request_handlers:
            prompts_capability = PromptsCapability(
                listChanged=notification_options.prompts_changed
            )

        # Set resource capabilities if handler exists
        if ListResourcesRequest in self.request_handlers:
            resources_capability = ResourcesCapability(
                subscribe=False, listChanged=notification_options.resources_changed
            )

        # Set tool capabilities if handler exists
        if ListToolsRequest in self.request_handlers:
            tools_capability = ToolsCapability(
                listChanged=notification_options.tools_changed
            )

        # Set logging capabilities if handler exists
        if SetLevelRequest in self.request_handlers:
            logging_capability = LoggingCapability()

        return ServerCapabilities(
            prompts=prompts_capability,
            resources=resources_capability,
            tools=tools_capability,
            logging=logging_capability,
            experimental=experimental_capabilities,
        )

    @property
    def request_context(self) -> RequestContext:
        """If called outside of a request context, this will raise a LookupError."""
        return request_ctx.get()

    def list_prompts(self):
        def decorator(func: Callable[[], Awaitable[list[Prompt]]]):
            logger.debug("Registering handler for PromptListRequest")

            async def handler(_: Any):
                prompts = await func()
                return ServerResult(ListPromptsResult(prompts=prompts))

            self.request_handlers[ListPromptsRequest] = handler
            return func

        return decorator

    def get_prompt(self):
        from mcp.types import (
            GetPromptRequest,
            GetPromptResult,
            ImageContent,
        )
        from mcp.types import (
            Role as Role,
        )

        def decorator(
            func: Callable[
                [str, dict[str, str] | None], Awaitable[types.PromptResponse]
            ],
        ):
            logger.debug("Registering handler for GetPromptRequest")

            async def handler(req: GetPromptRequest):
                prompt_get = await func(req.params.name, req.params.arguments)
                messages: list[PromptMessage] = []
                for message in prompt_get.messages:
                    match message.content:
                        case str() as text_content:
                            content = TextContent(type="text", text=text_content)
                        case types.ImageContent() as img_content:
                            content = ImageContent(
                                type="image",
                                data=img_content.data,
                                mimeType=img_content.mime_type,
                            )
                        case types.EmbeddedResource() as resource:
                            content = EmbeddedResource(
                                type="resource", resource=resource.resource
                            )
                        case _:
                            raise ValueError(
                                f"Unexpected content type: {type(message.content)}"
                            )

                    prompt_message = PromptMessage(role=message.role, content=content)
                    messages.append(prompt_message)

                return ServerResult(
                    GetPromptResult(description=prompt_get.desc, messages=messages)
                )

            self.request_handlers[GetPromptRequest] = handler
            return func

        return decorator

    def list_resources(self):
        def decorator(func: Callable[[], Awaitable[list[Resource]]]):
            logger.debug("Registering handler for ListResourcesRequest")

            async def handler(_: Any):
                resources = await func()
                return ServerResult(ListResourcesResult(resources=resources))

            self.request_handlers[ListResourcesRequest] = handler
            return func

        return decorator

    def read_resource(self):
        from mcp.types import (
            BlobResourceContents,
            TextResourceContents,
        )

        def decorator(func: Callable[[AnyUrl], Awaitable[str | bytes]]):
            logger.debug("Registering handler for ReadResourceRequest")

            async def handler(req: ReadResourceRequest):
                result = await func(req.params.uri)
                match result:
                    case str(s):
                        content = TextResourceContents(
                            uri=req.params.uri,
                            text=s,
                            mimeType="text/plain",
                        )
                    case bytes(b):
                        import base64

                        content = BlobResourceContents(
                            uri=req.params.uri,
                            blob=base64.urlsafe_b64encode(b).decode(),
                            mimeType="application/octet-stream",
                        )

                return ServerResult(
                    ReadResourceResult(
                        contents=[content],
                    )
                )

            self.request_handlers[ReadResourceRequest] = handler
            return func

        return decorator

    def set_logging_level(self):
        from mcp.types import EmptyResult

        def decorator(func: Callable[[LoggingLevel], Awaitable[None]]):
            logger.debug("Registering handler for SetLevelRequest")

            async def handler(req: SetLevelRequest):
                await func(req.params.level)
                return ServerResult(EmptyResult())

            self.request_handlers[SetLevelRequest] = handler
            return func

        return decorator

    def subscribe_resource(self):
        from mcp.types import EmptyResult

        def decorator(func: Callable[[AnyUrl], Awaitable[None]]):
            logger.debug("Registering handler for SubscribeRequest")

            async def handler(req: SubscribeRequest):
                await func(req.params.uri)
                return ServerResult(EmptyResult())

            self.request_handlers[SubscribeRequest] = handler
            return func

        return decorator

    def unsubscribe_resource(self):
        from mcp.types import EmptyResult

        def decorator(func: Callable[[AnyUrl], Awaitable[None]]):
            logger.debug("Registering handler for UnsubscribeRequest")

            async def handler(req: UnsubscribeRequest):
                await func(req.params.uri)
                return ServerResult(EmptyResult())

            self.request_handlers[UnsubscribeRequest] = handler
            return func

        return decorator

    def list_tools(self):
        def decorator(func: Callable[[], Awaitable[list[Tool]]]):
            logger.debug("Registering handler for ListToolsRequest")

            async def handler(_: Any):
                tools = await func()
                return ServerResult(ListToolsResult(tools=tools))

            self.request_handlers[ListToolsRequest] = handler
            return func

        return decorator

    def call_tool(self):
        from mcp.types import (
            CallToolResult,
            EmbeddedResource,
            ImageContent,
            TextContent,
        )

        def decorator(
            func: Callable[
                ...,
                Awaitable[Sequence[str | types.ImageContent | types.EmbeddedResource]],
            ],
        ):
            logger.debug("Registering handler for CallToolRequest")

            async def handler(req: CallToolRequest):
                try:
                    results = await func(req.params.name, (req.params.arguments or {}))
                    content = []
                    for result in results:
                        match result:
                            case str() as text:
                                content.append(TextContent(type="text", text=text))
                            case types.ImageContent() as img:
                                content.append(
                                    ImageContent(
                                        type="image",
                                        data=img.data,
                                        mimeType=img.mime_type,
                                    )
                                )
                            case types.EmbeddedResource() as resource:
                                content.append(
                                    EmbeddedResource(
                                        type="resource", resource=resource.resource
                                    )
                                )

                    return ServerResult(CallToolResult(content=content, isError=False))
                except Exception as e:
                    return ServerResult(
                        CallToolResult(
                            content=[TextContent(type="text", text=str(e))],
                            isError=True,
                        )
                    )

            self.request_handlers[CallToolRequest] = handler
            return func

        return decorator

    def progress_notification(self):
        def decorator(
            func: Callable[[str | int, float, float | None], Awaitable[None]],
        ):
            logger.debug("Registering handler for ProgressNotification")

            async def handler(req: ProgressNotification):
                await func(
                    req.params.progressToken, req.params.progress, req.params.total
                )

            self.notification_handlers[ProgressNotification] = handler
            return func

        return decorator

    def completion(self):
        """Provides completions for prompts and resource templates"""
        from mcp.types import CompleteResult, Completion, CompletionArgument

        def decorator(
            func: Callable[
                [PromptReference | ResourceReference, CompletionArgument],
                Awaitable[Completion | None],
            ],
        ):
            logger.debug("Registering handler for CompleteRequest")

            async def handler(req: CompleteRequest):
                completion = await func(req.params.ref, req.params.argument)
                return ServerResult(
                    CompleteResult(
                        completion=completion
                        if completion is not None
                        else Completion(values=[], total=None, hasMore=None),
                    )
                )

            self.request_handlers[CompleteRequest] = handler
            return func

        return decorator

    async def run(
        self,
        read_stream: MemoryObjectReceiveStream[JSONRPCMessage | Exception],
        write_stream: MemoryObjectSendStream[JSONRPCMessage],
        initialization_options: types.InitializationOptions,
        # When True, exceptions are returned as messages to the client.
        # When False, exceptions are raised, which will cause the server to shut down
        # but also make tracing exceptions much easier during testing and when using
        # in-process servers.
        raise_exceptions: bool = False,
    ):
        with warnings.catch_warnings(record=True) as w:
            async with ServerSession(
                read_stream, write_stream, initialization_options
            ) as session:
                async for message in session.incoming_messages:
                    logger.debug(f"Received message: {message}")

                    match message:
                        case RequestResponder(request=ClientRequest(root=req)):
                            logger.info(
                                f"Processing request of type {type(req).__name__}"
                            )
                            if type(req) in self.request_handlers:
                                handler = self.request_handlers[type(req)]
                                logger.debug(
                                    f"Dispatching request of type {type(req).__name__}"
                                )

                                token = None
                                try:
                                    # Set our global state that can be retrieved via
                                    # app.get_request_context()
                                    token = request_ctx.set(
                                        RequestContext(
                                            message.request_id,
                                            message.request_meta,
                                            session,
                                        )
                                    )
                                    response = await handler(req)
                                except Exception as err:
                                    if raise_exceptions:
                                        raise err
                                    response = ErrorData(
                                        code=0, message=str(err), data=None
                                    )
                                finally:
                                    # Reset the global state after we are done
                                    if token is not None:
                                        request_ctx.reset(token)

                                await message.respond(response)
                            else:
                                await message.respond(
                                    ErrorData(
                                        code=METHOD_NOT_FOUND,
                                        message="Method not found",
                                    )
                                )

                            logger.debug("Response sent")
                        case ClientNotification(root=notify):
                            if type(notify) in self.notification_handlers:
                                assert type(notify) in self.notification_handlers

                                handler = self.notification_handlers[type(notify)]
                                logger.debug(
                                    f"Dispatching notification of type "
                                    f"{type(notify).__name__}"
                                )

                                try:
                                    await handler(notify)
                                except Exception as err:
                                    logger.error(
                                        f"Uncaught exception in notification handler: "
                                        f"{err}"
                                    )

                    for warning in w:
                        logger.info(
                            f"Warning: {warning.category.__name__}: {warning.message}"
                        )


async def _ping_handler(request: PingRequest) -> ServerResult:
    return ServerResult(EmptyResult())