import json
import os
import sys
import urllib.request


port = int(
    os.environ.get(
        "WEB_PORT",
        "8090",
    )
)

url = (
    f"http://127.0.0.1:{port}/healthz"
)

try:
    with urllib.request.urlopen(
        url,
        timeout=4,
    ) as response:

        data = json.loads(
            response.read().decode(
                "utf-8"
            )
        )

except Exception as exc:
    print(
        f"Firestarter WebUI unhealthy: {exc}"
    )
    sys.exit(1)


if data.get("status") != "ok":
    print(
        "Firestarter reported unhealthy"
    )
    sys.exit(1)


mode = data.get(
    "mode",
    "runtime",
)

print(
    f"healthy: Firestarter mode={mode}"
)

sys.exit(0)
