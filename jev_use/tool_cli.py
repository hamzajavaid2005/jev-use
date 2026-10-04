"""Built-in fallback for harnesses that fail to load MCP tools."""
import argparse
import base64
import json
import sys
import tempfile
from . import mcp_server

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tool", choices=list(mcp_server.HANDLERS))
    for name in ("profile", "url", "goal", "serial", "vendor", "action", "account", "label", "text", "code", "resume-token", "run-id", "place", "audience"):
        parser.add_argument("--" + name)
    parser.add_argument("--port", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--chunk-size", type=int)
    parser.add_argument("--chunk-budget-seconds", type=float)
    parser.add_argument("--include-screenshot", action="store_true", default=None)
    parser.add_argument("--act", action="store_true", default=None)
    parser.add_argument("--continue-after-blocker", action="store_true", default=None)
    parser.add_argument("--retry-current", action="store_true", default=None)
    parser.add_argument("--publish", action="store_true", default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--json-file", help="Advanced arguments as JSON in a file; avoids shell quoting.")
    options = vars(parser.parse_args(argv))
    tool = options.pop("tool")
    json_file = options.pop("json_file")
    arguments = {}
    if json_file:
        with open(json_file, encoding="utf-8") as source:
            arguments = json.load(source)
    arguments.update({k: v for k, v in options.items() if v is not None})
    schema = next(t["inputSchema"] for t in mcp_server.TOOLS if t["name"] == tool)
    missing = [name for name in schema.get("required", []) if name not in arguments]
    if missing:
        parser.error("missing arguments: " + ", ".join(missing))
    mcp_server.load_env()
    response = mcp_server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    result = response["result"]
    for block in result["content"]:
        if block.get("type") == "image":
            with tempfile.NamedTemporaryFile(prefix="jev-phone-", suffix=".png", delete=False) as capture:
                capture.write(base64.b64decode(block["data"], validate=True))
                print("screenshot=" + capture.name)
        else:
            print(block.get("text", ""))
    return 1 if result.get("isError") else 0

if __name__ == "__main__":
    sys.exit(main())
