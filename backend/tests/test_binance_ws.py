import asyncio
import contextlib
import json

from websockets.exceptions import ConnectionClosed

from websockets.asyncio.server import serve

from app.data.providers.binance_ws import BinanceStreamManager


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def manager(port, received, **kw):
    return BinanceStreamManager(
        [f"ws://127.0.0.1:{port}"],
        lambda stream, data: received.append((stream, data)),
        backoff_base_seconds=0.05,
        backoff_max_seconds=0.1,
        connect_kwargs={"proxy": None, "ping_interval": None},
        **kw,
    )


async def test_subscribes_dispatches_and_resubscribes_after_disconnect():
    subscriptions = []
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        msg = json.loads(await ws.recv())
        subscriptions.append(msg)
        await ws.send(json.dumps({"result": None, "id": msg["id"]}))
        await ws.send(json.dumps({"stream": "btcusdt@miniTicker", "data": {"s": "BTCUSDT", "c": "1.0"}}))
        if connections == 1:
            await ws.close()
            return
        await ws.wait_closed()

    received = []
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        mgr = manager(port, received)
        await mgr.start(["btcusdt@miniTicker", "ethusdt@miniTicker"])
        assert await wait_for(lambda: len(received) >= 2)
        status = mgr.status()
        await mgr.stop()

    assert len(subscriptions) >= 2
    assert all(s["method"] == "SUBSCRIBE" for s in subscriptions)
    assert subscriptions[0]["params"] == ["btcusdt@miniTicker", "ethusdt@miniTicker"]
    assert subscriptions[1]["params"] == subscriptions[0]["params"]
    assert status.reconnects >= 1 and status.messages_received >= 2
    assert received[0] == ("btcusdt@miniTicker", {"s": "BTCUSDT", "c": "1.0"})
    assert mgr.running is False


async def test_stale_stream_triggers_reconnect():
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        await ws.recv()
        await ws.wait_closed()  # silent: no market data until the client gives up

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        mgr = manager(port, [], stale_after_seconds=0.4)
        await mgr.start(["btcusdt@miniTicker"])
        assert await wait_for(lambda: connections >= 2)
        status = mgr.status()
        await mgr.stop()
    assert status.reconnects >= 1
    assert "stale" in (status.last_error or "")


async def test_connection_is_renewed_before_lifetime_limit():
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        await ws.recv()
        with contextlib.suppress(ConnectionClosed):
            for _ in range(100):
                await ws.send(json.dumps({"stream": "btcusdt@miniTicker", "data": {"s": "BTCUSDT", "c": "1"}}))
                await asyncio.sleep(0.05)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        mgr = manager(port, [], max_connection_seconds=0.5)
        await mgr.start(["btcusdt@miniTicker"])
        assert await wait_for(lambda: mgr.status().renewals >= 1)
        await mgr.stop()
    assert connections >= 2


async def test_update_streams_sends_diff():
    messages = []

    async def handler(ws):
        async for raw in ws:
            messages.append(json.loads(raw))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        mgr = manager(port, [], stale_after_seconds=5)
        await mgr.start(["btcusdt@miniTicker"])
        assert await wait_for(lambda: len(messages) >= 1)
        await mgr.update_streams(["ethusdt@miniTicker"])
        assert await wait_for(lambda: len(messages) >= 3)
        await mgr.stop()
    assert messages[1] == {"method": "UNSUBSCRIBE", "params": ["btcusdt@miniTicker"], "id": 2}
    assert messages[2] == {"method": "SUBSCRIBE", "params": ["ethusdt@miniTicker"], "id": 3}


async def test_stop_is_prompt_while_backing_off():
    received = []
    mgr = BinanceStreamManager(
        ["ws://127.0.0.1:9"],  # nothing listens here
        lambda s, d: received.append(s),
        backoff_base_seconds=30,
        backoff_max_seconds=30,
        connect_kwargs={"proxy": None, "open_timeout": 1},
    )
    await mgr.start(["btcusdt@miniTicker"])
    assert await wait_for(lambda: mgr.status().reconnects >= 1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await mgr.stop()
    assert loop.time() - started < 2
