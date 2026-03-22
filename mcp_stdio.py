#!/usr/bin/env python3
import json
import sys

import httpx


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = httpx.post(
                "http://localhost:8000/mcp",
                json=request,
                timeout=30
            )
            data = response.json()
            if data:  # skip empty responses (notifications)
                sys.stdout.write(json.dumps(data) + "\n")
                sys.stdout.flush()
        except Exception as e:
            error = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32000, "message": str(e)}}
            sys.stdout.write(json.dumps(error) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()