#!/usr/bin/env python3
"""
MCP server for live tag access to Rockwell Automation Studio 5000 / Logix5000
controllers (ControlLogix, CompactLogix) over EtherNet/IP (CIP), via pycomm3.

This talks directly to a running controller on the network. It does not
require Studio 5000 Logix Designer to be installed, and it does not touch
project files (.ACD/.L5X) or the Logix Designer application itself.
"""

import asyncio
import atexit
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from pycomm3 import CIPDriver, LogixDriver
from pydantic import BaseModel, ConfigDict, Field, field_validator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("studio5000_mcp")

mcp = FastMCP("studio5000_mcp")

# ---------------------------------------------------------------------------
# Connection management
#
# LogixDriver connections are cached per CIP path so repeated tool calls
# reuse the same session instead of paying connection setup cost every time.
# Access to each cached driver is serialized with an asyncio.Lock because the
# underlying pycomm3 socket is not safe for concurrent use. If an operation
# raises, the cached driver is closed and evicted so the next call reconnects
# instead of reusing a socket left in an unknown state.
# ---------------------------------------------------------------------------

_drivers: Dict[str, LogixDriver] = {}
_driver_locks: Dict[str, asyncio.Lock] = {}
_registry_lock = asyncio.Lock()


async def _get_lock(path: str) -> asyncio.Lock:
    async with _registry_lock:
        lock = _driver_locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            _driver_locks[path] = lock
        return lock


def _open_driver(path: str) -> LogixDriver:
    driver = LogixDriver(path)
    driver.open()
    return driver


def _close_driver(driver: LogixDriver) -> None:
    try:
        driver.close()
    except Exception:
        pass


@asynccontextmanager
async def _driver_session(path: str):
    """Yield a connected, cached LogixDriver for `path`."""
    lock = await _get_lock(path)
    async with lock:
        driver = _drivers.get(path)
        if driver is None or not driver.connected:
            if driver is not None:
                await asyncio.to_thread(_close_driver, driver)
            driver = await asyncio.to_thread(_open_driver, path)
            _drivers[path] = driver
        try:
            yield driver
        except Exception:
            await asyncio.to_thread(_close_driver, driver)
            _drivers.pop(path, None)
            raise


def _close_all_drivers() -> None:
    for driver in list(_drivers.values()):
        _close_driver(driver)


atexit.register(_close_all_drivers)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Recursively coerce pycomm3 return values into JSON-serializable data."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items() if k != "type_class"}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _error_response(e: Exception, **context: Any) -> str:
    payload = {"error": f"{type(e).__name__}: {e}", **context}
    return json.dumps(payload, indent=2)


PATH_DESCRIPTION = (
    "CIP path to the controller. A plain IP assumes backplane slot 0 "
    "(e.g. '192.168.1.10'). Append '/<slot>' for a specific ControlLogix "
    "backplane slot (e.g. '192.168.1.10/1'). Full routed CIP paths are also "
    "supported per pycomm3 conventions."
)


class PlcPathInput(BaseModel):
    """Base input model carrying the controller's CIP path."""

    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid"
    )

    path: str = Field(..., description=PATH_DESCRIPTION, min_length=1, max_length=200)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path cannot be empty")
        return v.strip()


class GetControllerInfoInput(PlcPathInput):
    pass


class ListTagsInput(PlcPathInput):
    program: Optional[str] = Field(
        default=None,
        description=(
            "Tag scope: omit for controller-scope tags only, '*' for all "
            "controller and program tags, or a specific program name (e.g. "
            "'MainProgram') for that program's local tags."
        ),
        max_length=100,
    )


class ReadTagInput(PlcPathInput):
    tag: str = Field(
        ...,
        description=(
            "Tag name to read, e.g. 'MyTag', 'MyArray[3]', 'MyArray{10}' for "
            "10 elements starting at index 0, or 'MyUDT.Member'."
        ),
        min_length=1,
        max_length=300,
    )


class ReadTagsInput(PlcPathInput):
    tags: List[str] = Field(
        ...,
        description="Tag names to read in a single batch request.",
        min_length=1,
        max_length=200,
    )


class TagValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag: str = Field(
        ..., description="Tag name to write, e.g. 'MyTag' or 'MyArray[3]'.",
        min_length=1, max_length=300,
    )
    value: Any = Field(
        ...,
        description=(
            "Value to write, matching the tag's data type: true/false for "
            "BOOL, a number for integer/float types, a string for STRING "
            "tags, a list for array writes, or an object for UDT member "
            "writes."
        ),
    )


class WriteTagInput(PlcPathInput):
    tag: str = Field(
        ..., description="Tag name to write, e.g. 'MyTag' or 'MyArray[3]'.",
        min_length=1, max_length=300,
    )
    value: Any = Field(
        ...,
        description=(
            "Value to write, matching the tag's data type: true/false for "
            "BOOL, a number for integer/float types, a string for STRING "
            "tags, a list for array writes, or an object for UDT member "
            "writes."
        ),
    )


class WriteTagsInput(PlcPathInput):
    tags: List[TagValue] = Field(
        ...,
        description="{tag, value} pairs to write in a single batch request.",
        min_length=1,
        max_length=200,
    )


class DisconnectInput(PlcPathInput):
    pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="studio5000_discover_plcs",
    annotations={
        "title": "Discover PLCs on the Network",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def studio5000_discover_plcs() -> str:
    """Broadcast an EtherNet/IP "List Identity" request to find controllers on the local network.

    Use this to find a ControlLogix/CompactLogix controller's IP address when
    it isn't already known, before calling the other studio5000_* tools. Only
    finds devices reachable by broadcast from the machine running this MCP
    server (does not cross routers/subnets).

    Args:
        (none)

    Returns:
        str: JSON array of discovered devices, each an EtherNet/IP Identity
        Object as a dict (fields vary by device but typically include
        ip_address, product_name, vendor, device_type, revision,
        serial_number). Returns "[]" if none are found.

    Examples:
        - Use when: "What PLCs are on the network?" or "I don't know the controller's IP."
        - Don't use when: The IP/path is already known (call studio5000_get_controller_info directly).
    """
    try:
        devices = await asyncio.to_thread(CIPDriver.discover)
        return json.dumps(_json_safe(devices), indent=2)
    except Exception as e:
        return _error_response(e)


@mcp.tool(
    name="studio5000_get_controller_info",
    annotations={
        "title": "Get Controller Identity Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def studio5000_get_controller_info(params: GetControllerInfoInput) -> str:
    """Connect to a Logix5000 controller and return its identity information.

    Confirms reachability and identifies the controller (name, product type,
    revision, serial number, vendor, etc.) before doing tag work.

    Args:
        params (GetControllerInfoInput): Validated input containing:
            - path (str): CIP path to the controller.

    Returns:
        str: JSON object: {"path": str, "info": {...controller identity
        fields...}}. On failure: {"error": str, "path": str}.

    Examples:
        - Use when: "Confirm we can reach the PLC at 192.168.1.10" or
          "What controller is this / what firmware revision?"
        - Don't use when: You need tag values (use studio5000_read_tag(s)) or
          the full tag list (use studio5000_list_tags).
    """
    try:
        async with _driver_session(params.path) as driver:
            info = await asyncio.to_thread(lambda: dict(driver.info))
        return json.dumps({"path": params.path, "info": _json_safe(info)}, indent=2)
    except Exception as e:
        return _error_response(e, path=params.path)


@mcp.tool(
    name="studio5000_list_tags",
    annotations={
        "title": "List Controller/Program Tags",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def studio5000_list_tags(params: ListTagsInput) -> str:
    """List tag definitions uploaded live from a Logix5000 controller.

    Returns each tag's name, atomic/struct classification, data type, array
    dimensions, external access level, and whether it's an alias. Use this to
    discover exact tag names/types before reading or writing them.

    Args:
        params (ListTagsInput): Validated input containing:
            - path (str): CIP path to the controller.
            - program (Optional[str]): None for controller-scope tags only,
              '*' for all controller + program tags, or a program name for
              that program's local tags.

    Returns:
        str: JSON object: {"path": str, "scope": str, "count": int, "tags":
        [{"tag_name": str, "tag_type": "atomic"|"struct", "data_type": str|dict,
        "dim": int, "dimensions": [int, ...], "external_access": str,
        "alias": bool}, ...]}. On failure: {"error": str, "path": str}.

    Examples:
        - Use when: "What tags exist on this controller?" or "What's the
          exact name/type of the conveyor speed tag?"
        - Don't use when: You already know the tag name and just need its
          value (use studio5000_read_tag).
    """
    try:
        async with _driver_session(params.path) as driver:
            tag_defs = await asyncio.to_thread(driver.get_tag_list, params.program)
        tags = [
            {
                "tag_name": t.get("tag_name"),
                "tag_type": t.get("tag_type"),
                "data_type": _json_safe(t.get("data_type")),
                "dim": t.get("dim"),
                "dimensions": _json_safe(t.get("dimensions")),
                "external_access": t.get("external_access"),
                "alias": t.get("alias"),
            }
            for t in tag_defs
        ]
        return json.dumps(
            {
                "path": params.path,
                "scope": params.program or "controller",
                "count": len(tags),
                "tags": tags,
            },
            indent=2,
        )
    except Exception as e:
        return _error_response(e, path=params.path)


@mcp.tool(
    name="studio5000_read_tag",
    annotations={
        "title": "Read a PLC Tag",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def studio5000_read_tag(params: ReadTagInput) -> str:
    """Read the current live value of a single tag from a Logix5000 controller.

    Args:
        params (ReadTagInput): Validated input containing:
            - path (str): CIP path to the controller.
            - tag (str): Tag name, optionally with array/member syntax
              (e.g. 'MyArray[3]', 'MyUDT.Member').

    Returns:
        str: JSON object on success: {"path": str, "tag": str, "value": Any,
        "type": str, "success": true}. On a CIP-level read failure (e.g. tag
        doesn't exist): {"path": str, "tag": str, "success": false, "error":
        str}. On a connection/exception failure: {"error": str, "path": str,
        "tag": str}.

    Examples:
        - Use when: "What's the current value of Conveyor1_Speed?"
        - Don't use when: Reading many tags at once (use studio5000_read_tags
          for a batch request instead of many single calls).
    """
    try:
        async with _driver_session(params.path) as driver:
            result = await asyncio.to_thread(driver.read, params.tag)
        if result.error:
            return json.dumps(
                {
                    "path": params.path,
                    "tag": params.tag,
                    "success": False,
                    "error": result.error,
                },
                indent=2,
            )
        return json.dumps(
            {
                "path": params.path,
                "tag": result.tag,
                "value": _json_safe(result.value),
                "type": result.type,
                "success": True,
            },
            indent=2,
        )
    except Exception as e:
        return _error_response(e, path=params.path, tag=params.tag)


@mcp.tool(
    name="studio5000_read_tags",
    annotations={
        "title": "Read Multiple PLC Tags",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def studio5000_read_tags(params: ReadTagsInput) -> str:
    """Read the current live values of multiple tags in a single batch request.

    More efficient than calling studio5000_read_tag repeatedly since all tags
    are read in one round trip to the controller.

    Args:
        params (ReadTagsInput): Validated input containing:
            - path (str): CIP path to the controller.
            - tags (List[str]): Tag names to read.

    Returns:
        str: JSON object: {"path": str, "count": int, "results": [{"tag": str,
        "value": Any, "type": str, "success": bool, "error": Optional[str]},
        ...]}. Each tag's success/error is reported independently -- one bad
        tag name does not fail the whole batch. On a connection/exception
        failure: {"error": str, "path": str, "tags": [...]}.

    Examples:
        - Use when: "Read Conveyor1_Speed, Conveyor2_Speed, and LineFault
          together."
        - Don't use when: Only one tag is needed (use studio5000_read_tag).
    """
    try:
        async with _driver_session(params.path) as driver:
            raw = await asyncio.to_thread(driver.read, *params.tags)
        results = raw if isinstance(raw, (list, tuple)) else [raw]
        out = [
            {
                "tag": r.tag,
                "value": _json_safe(r.value) if not r.error else None,
                "type": r.type,
                "success": r.error is None,
                "error": r.error,
            }
            for r in results
        ]
        return json.dumps({"path": params.path, "count": len(out), "results": out}, indent=2)
    except Exception as e:
        return _error_response(e, path=params.path, tags=params.tags)


@mcp.tool(
    name="studio5000_write_tag",
    annotations={
        "title": "Write a PLC Tag",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def studio5000_write_tag(params: WriteTagInput) -> str:
    """Write a value to a single tag on a live Logix5000 controller.

    This changes state on a real, running controller and can affect connected
    physical equipment immediately. There is no simulation/dry-run mode and
    no write allowlist -- any tag path reachable and writable on the target
    controller will be written as requested.

    Args:
        params (WriteTagInput): Validated input containing:
            - path (str): CIP path to the controller.
            - tag (str): Tag name to write, optionally with array/member
              syntax (e.g. 'MyArray[3]', 'MyUDT.Member').
            - value (Any): Value matching the tag's data type (bool, number,
              string, list, or object).

    Returns:
        str: JSON object: {"path": str, "tag": str, "value_written": Any,
        "success": bool, "error": Optional[str]}. On a connection/exception
        failure: {"error": str, "path": str, "tag": str}.

    Examples:
        - Use when: "Set Conveyor1_SpeedSetpoint to 50.0" or "Reset the
          fault-acknowledge bit."
        - Don't use when: Writing several tags together (use
          studio5000_write_tags to batch them in one round trip).
    """
    try:
        async with _driver_session(params.path) as driver:
            result = await asyncio.to_thread(driver.write, (params.tag, params.value))
        payload = {
            "path": params.path,
            "tag": result.tag,
            "value_written": params.value,
            "success": result.error is None,
        }
        if result.error:
            payload["error"] = result.error
        return json.dumps(payload, indent=2)
    except Exception as e:
        return _error_response(e, path=params.path, tag=params.tag)


@mcp.tool(
    name="studio5000_write_tags",
    annotations={
        "title": "Write Multiple PLC Tags",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def studio5000_write_tags(params: WriteTagsInput) -> str:
    """Write values to multiple tags on a live Logix5000 controller in one batch request.

    Same caveats as studio5000_write_tag: this changes state on a real,
    running controller with no simulation/dry-run mode or write allowlist.

    Args:
        params (WriteTagsInput): Validated input containing:
            - path (str): CIP path to the controller.
            - tags (List[TagValue]): {tag, value} pairs to write.

    Returns:
        str: JSON object: {"path": str, "count": int, "results": [{"tag": str,
        "success": bool, "error": Optional[str]}, ...]}. Each tag's
        success/error is reported independently. On a connection/exception
        failure: {"error": str, "path": str, "tags": [...]}.

    Examples:
        - Use when: "Set SpeedSetpoint to 50.0 and AutoMode to true together."
        - Don't use when: Only one tag needs writing (use
          studio5000_write_tag).
    """
    try:
        pairs = [(tv.tag, tv.value) for tv in params.tags]
        async with _driver_session(params.path) as driver:
            raw = await asyncio.to_thread(driver.write, *pairs)
        results = raw if isinstance(raw, (list, tuple)) else [raw]
        out = [{"tag": r.tag, "success": r.error is None, "error": r.error} for r in results]
        return json.dumps({"path": params.path, "count": len(out), "results": out}, indent=2)
    except Exception as e:
        return _error_response(e, path=params.path, tags=[tv.tag for tv in params.tags])


@mcp.tool(
    name="studio5000_disconnect",
    annotations={
        "title": "Close a Cached PLC Connection",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def studio5000_disconnect(params: DisconnectInput) -> str:
    """Close and evict a cached connection to a controller.

    Connections are opened lazily and cached per CIP path across tool calls.
    This is normally not needed, but is useful to release a controller's CIP
    connection slot explicitly (they are limited) or to force the next call
    to that path to reconnect from scratch.

    Args:
        params (DisconnectInput): Validated input containing:
            - path (str): CIP path of the connection to close.

    Returns:
        str: JSON object: {"path": str, "disconnected": bool} -- false if
        there was no cached connection for that path.

    Examples:
        - Use when: "Done with the PLC at 192.168.1.10, release the connection."
        - Don't use when: You plan to read/write that path again shortly
          (the connection is reused automatically otherwise).
    """
    lock = await _get_lock(params.path)
    async with lock:
        driver = _drivers.pop(params.path, None)
        if driver is not None:
            await asyncio.to_thread(_close_driver, driver)
    return json.dumps({"path": params.path, "disconnected": driver is not None}, indent=2)


if __name__ == "__main__":
    mcp.run()
