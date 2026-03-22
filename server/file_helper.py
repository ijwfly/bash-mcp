#!/usr/bin/env python3
"""Standalone helper for file operations, executed via sudo -u <user>.

Protocol: reads a single JSON object from stdin, writes a JSON result to stdout.

Operations:
  {"op": "read",  "path": "...", "limit": 0}
  {"op": "write", "path": "...", "content": "..."}
  {"op": "edit",  "path": "...", "old_text": "...", "new_text": "..."}
"""

import json
import os
import sys


def do_read(path: str, limit: int = 0) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except IsADirectoryError:
        return {"error": f"Is a directory: {path}"}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}

    lines = content.splitlines(keepends=True)
    if limit > 0:
        lines = lines[:limit]
        content = "".join(lines)

    return {
        "content": content,
        "size": os.path.getsize(path),
        "lines": len(lines),
    }


def do_write(path: str, content: str) -> dict:
    try:
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except IsADirectoryError:
        return {"error": f"Is a directory: {path}"}

    return {
        "status": "ok",
        "size": os.path.getsize(path),
        "path": path,
    }


def do_edit(path: str, old_text: str, new_text: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except IsADirectoryError:
        return {"error": f"Is a directory: {path}"}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}

    count = content.count(old_text)
    if count == 0:
        return {"error": "old_text not found"}
    if count > 1:
        return {"error": f"old_text found {count} times, must be unique"}

    new_content = content.replace(old_text, new_text, 1)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except PermissionError:
        return {"error": f"Permission denied: {path}"}

    return {"status": "ok", "replacements": 1}


def main():
    req = json.loads(sys.stdin.read())
    op = req.get("op")
    if op == "read":
        result = do_read(req["path"], req.get("limit", 0))
    elif op == "write":
        result = do_write(req["path"], req["content"])
    elif op == "edit":
        result = do_edit(req["path"], req["old_text"], req["new_text"])
    else:
        result = {"error": f"Unknown op: {op}"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
