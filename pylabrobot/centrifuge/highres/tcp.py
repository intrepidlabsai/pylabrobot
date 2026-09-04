"""Minimal asyncio TCP client used by MicroSpinBackend.

The public pylabrobot tree later introduced ``pylabrobot.io.socket.Socket``.
This branch is based on ``edits``, which does not have that IO package, so
the MicroSpin backend talks through this small local wrapper instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class Socket:
  """Read/write a TCP connection with per-call timeouts."""

  def __init__(
    self,
    human_readable_device_name: str,
    host: str,
    port: int,
    read_timeout: float = 30,
    write_timeout: float = 30,
  ):
    self.human_readable_device_name = human_readable_device_name
    self._host = host
    self._port = port
    self._reader: Optional[asyncio.StreamReader] = None
    self._writer: Optional[asyncio.StreamWriter] = None
    self._read_timeout = read_timeout
    self._write_timeout = write_timeout
    self._read_lock = asyncio.Lock()
    self._write_lock = asyncio.Lock()

  async def setup(self) -> None:
    self._reader, self._writer = await asyncio.open_connection(self._host, self._port)

  async def stop(self) -> None:
    reader_writer = self._writer
    self._reader = None
    self._writer = None
    if reader_writer is None:
      return
    try:
      reader_writer.close()
      await reader_writer.wait_closed()
    except OSError as exc:
      logger.warning("Error while closing socket connection: %s", exc)

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    if self._writer is None:
      raise RuntimeError(
        f"Socket for '{self.human_readable_device_name}' not set up; call setup() first"
      )
    timeout = self._write_timeout if timeout is None else timeout
    async with self._write_lock:
      self._writer.write(data)
      try:
        await asyncio.wait_for(self._writer.drain(), timeout=timeout)
      except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Timeout while writing to socket after {timeout} seconds") from exc

  async def readline(self, timeout: Optional[float] = None) -> bytes:
    if self._reader is None:
      raise RuntimeError(
        f"Socket for '{self.human_readable_device_name}' not set up; call setup() first"
      )
    timeout = self._read_timeout if timeout is None else timeout
    async with self._read_lock:
      try:
        return await asyncio.wait_for(self._reader.readline(), timeout=timeout)
      except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Timeout while reading from socket after {timeout} seconds") from exc
