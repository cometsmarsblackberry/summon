"""Helpers for outbound HTTP requests to Steam."""

import httpx


def create_steam_async_client(timeout: httpx.Timeout | float | None = 10.0) -> httpx.AsyncClient:
    """Return an AsyncClient configured for Steam's flaky address-family path.

    Forcing an IPv4 local address avoids hanging on broken IPv6 paths that can
    show up in container/VPS environments.
    """
    try:
        transport = httpx.AsyncHTTPTransport(
            local_address="0.0.0.0",
            retries=1,
        )
    except TypeError:
        transport = httpx.AsyncHTTPTransport(retries=1)
    return httpx.AsyncClient(
        timeout=timeout,
        transport=transport,
        trust_env=False,
    )
