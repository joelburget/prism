"""One-request JSON adapter; each request owns a fresh simulator."""
import json
import sys
from workflow import DomainError, Simulator, parse_workflow


def main() -> None:
    try:
        request = json.load(sys.stdin)
        result = Simulator(parse_workflow(request["input"])).run()
        response = {"ok": True, "result": result}
    except DomainError as error:
        response = {"ok": False, "error": {"code": error.code}}
    except (ValueError, KeyError, TypeError):
        response = {"ok": False, "error": {"code": "INVALID_INPUT"}}
    print(json.dumps(response, separators=(",", ":")))


if __name__ == "__main__":
    main()
