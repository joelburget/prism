"""One-request JSON adapter; each request owns a fresh simulator."""
import json
import sys
from workflow import DomainError, LeasedSimulator, Simulator, parse_leased_workflow, parse_workflow


def main() -> None:
    try:
        request = json.load(sys.stdin)
        raw_input = request["input"]
        if type(raw_input) is dict and "workers" in raw_input:
            result = LeasedSimulator(parse_leased_workflow(raw_input)).run()
        else:
            result = Simulator(parse_workflow(raw_input)).run()
        response = {"ok": True, "result": result}
    except DomainError as error:
        response = {"ok": False, "error": {"code": error.code}}
    except (ValueError, KeyError, TypeError):
        response = {"ok": False, "error": {"code": "INVALID_INPUT"}}
    print(json.dumps(response, separators=(",", ":")))


if __name__ == "__main__":
    main()
