import asyncio
import os

from mcp.server.fastmcp import FastMCP

BASH_TIMEOUT_MAX = int(os.environ.get("BASH_TIMEOUT_MAX", "300"))
PORT = int(os.environ.get("MCP_PORT", "8080"))

mcp = FastMCP(
    "bash-mcp",
    stateless_http=True,
    host="0.0.0.0",
    port=PORT,
)


@mcp.tool()
async def bash_exec(command: str, timeout: int = 30) -> dict:
    """Executes a bash command in a subprocess. Each call is stateless —
    a new subshell is spawned every time, so state does not persist
    between calls (no shared environment variables, working directory
    resets to /workspace on each invocation). Chain dependent commands
    with && or write a script if you need stateful execution."""
    timeout = max(1, min(timeout, BASH_TIMEOUT_MAX))

    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/workspace",
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
        }
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "exit_code": -1,
        }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
