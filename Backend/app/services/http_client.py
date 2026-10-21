import asyncio
import weakref

import httpx

shared_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None
_clients_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


async def initialize_shared_client() -> httpx.AsyncClient:
    """Create or reuse the connection pool owned by the current event loop."""
    global shared_client, _client_loop
    loop = asyncio.get_running_loop()
    client = _clients_by_loop.get(loop)
    if client is None or client.is_closed:
        if _client_loop is None and shared_client is not None and not shared_client.is_closed:
            client = shared_client
        else:
            client = _new_client()
        _clients_by_loop[loop] = client
    shared_client = client
    _client_loop = loop
    return client


def get_shared_client() -> httpx.AsyncClient:
    global shared_client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if shared_client is None or shared_client.is_closed:
            shared_client = _new_client()
        return shared_client

    client = _clients_by_loop.get(loop)
    if client is not None and not client.is_closed:
        shared_client = client
        _client_loop = loop
        return client

    if _client_loop in {None, loop} and shared_client is not None and not shared_client.is_closed:
        client = shared_client
    else:
        client = _new_client()
    _client_loop = loop
    _clients_by_loop[loop] = client
    shared_client = client
    return client


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,
        limits=httpx.Limits(max_keepalive_connections=50, max_connections=100),
        timeout=3.0,
    )


async def close_shared_client() -> None:
    global shared_client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    client = _clients_by_loop.pop(loop, None) if loop is not None else shared_client
    if client is not None and not client.is_closed:
        await client.aclose()
    if shared_client is client:
        shared_client = None
    if _client_loop is loop:
        _client_loop = None
