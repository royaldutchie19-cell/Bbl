"""Minimal Polygon JSON-RPC client for USDC Transfer log scraping.

We only need three RPC methods:
  - eth_blockNumber (find current head)
  - eth_getLogs    (scan USDC Transfer events filtered by recipient)
  - eth_getBlockByNumber (look up timestamp for a block, used sparingly)

USDC's Transfer event:
    Transfer(address indexed from, address indexed to, uint256 value)
    topic[0] = keccak256("Transfer(address,address,uint256)")
            = 0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef

To filter by recipient, we put the recipient (left-padded to 32 bytes) in
topic[2].
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bbl.config import OnchainConfig

log = logging.getLogger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _topic_addr(addr: str) -> str:
    """Address → 32-byte topic (left-padded, lowercase, 0x-prefixed)."""
    a = addr.lower()
    if a.startswith("0x"):
        a = a[2:]
    return "0x" + a.rjust(64, "0")


def _addr_from_topic(topic: str) -> str:
    """Strip 32-byte topic back to a 20-byte address."""
    return "0x" + topic[-40:].lower()


class OnchainClient:
    def __init__(self, cfg: OnchainConfig):
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> OnchainClient:
        self._client = httpx.AsyncClient(timeout=self.cfg.request_timeout_s)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def _rpc(self, method: str, params: list) -> Any:
        async with self._lock:
            self._id += 1
            req_id = self._id
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type((httpx.TransportError, _RpcRetry)),
            reraise=True,
        ):
            with attempt:
                assert self._client
                r = await self._client.post(self.cfg.rpc_url, json=payload)
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    msg = str(data["error"])
                    # public RPCs return "limit exceeded" / "block range too wide"
                    if "limit" in msg.lower() or "rate" in msg.lower():
                        await asyncio.sleep(2)
                        raise _RpcRetry(msg)
                    raise RuntimeError(f"rpc error: {msg}")
                return data.get("result")
        return None

    async def block_number(self) -> int:
        result = await self._rpc("eth_blockNumber", [])
        return int(result, 16)

    async def get_logs(
        self,
        *,
        address: str,
        from_block: int,
        to_block: int,
        topics: list,
    ) -> list[dict]:
        params = [
            {
                "address": address,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "topics": topics,
            }
        ]
        result = await self._rpc("eth_getLogs", params)
        return result or []

    async def get_block_timestamp(self, block_number: int) -> int | None:
        result = await self._rpc(
            "eth_getBlockByNumber", [hex(block_number), False]
        )
        if not result:
            return None
        return int(result["timestamp"], 16)

    # ---------------- high-level helper ----------------

    async def fetch_inbound_usdc(
        self,
        proxy_wallet: str,
        *,
        from_block: int,
        to_block: int,
    ) -> list[dict]:
        """Return all USDC Transfer events into `proxy_wallet` in [from..to].

        Walks the range in `log_block_step`-sized chunks. Each returned dict:
            {block, tx_hash, log_index, from, to, value, usdc_address}
        """
        topics = [TRANSFER_TOPIC, None, _topic_addr(proxy_wallet)]
        out: list[dict] = []
        for usdc in self.cfg.usdc_addresses:
            cursor = from_block
            while cursor <= to_block:
                end = min(cursor + self.cfg.log_block_step - 1, to_block)
                try:
                    logs = await self.get_logs(
                        address=usdc,
                        from_block=cursor,
                        to_block=end,
                        topics=topics,
                    )
                except Exception as e:
                    log.warning(
                        "getLogs %s [%d..%d] failed: %s — halving step",
                        usdc[:10], cursor, end, e,
                    )
                    # Halve the window and retry once
                    half = (cursor + end) // 2
                    try:
                        logs_a = await self.get_logs(
                            address=usdc, from_block=cursor, to_block=half, topics=topics,
                        )
                        logs_b = await self.get_logs(
                            address=usdc, from_block=half + 1, to_block=end, topics=topics,
                        )
                        logs = logs_a + logs_b
                    except Exception as e2:
                        log.warning("retry also failed: %s — skipping window", e2)
                        logs = []
                for entry in logs:
                    out.append(
                        {
                            "block": int(entry["blockNumber"], 16),
                            "tx_hash": entry["transactionHash"],
                            "log_index": int(entry["logIndex"], 16),
                            "from": _addr_from_topic(entry["topics"][1]),
                            "to": _addr_from_topic(entry["topics"][2]),
                            "value_raw": int(entry["data"], 16),
                            "usdc_address": usdc,
                        }
                    )
                cursor = end + 1
        return out


class _RpcRetry(Exception):
    """Retryable RPC issue (rate limit, range too wide)."""
