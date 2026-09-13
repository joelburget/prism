"""One-request JSON adapter; each request owns a fresh simulator.

Requests with a `workers` field use the checkpoint-two leased-worker mode; all other
requests use the unchanged checkpoint-one contract.
"""
import json
import sys
from leased import LeasedSimulator, parse_leased_workflow
from workflow import DomainError, Simulator, parse_workflow


def main() -> None:
    try:
        request = json.load(sys.stdin)
        payload = request["input"]
        if type(payload) is dict and "workers" in payload:
            result = LeasedSimulator(parse_leased_workflow(payload)).run()
        else:
            result = Simulator(parse_workflow(payload)).run()
        response = {"ok": True, "result": result}
    except DomainError as error:
        response = {"ok": False, "error": {"code": error.code}}
    except (ValueError, KeyError, TypeError):
        response = {"ok": False, "error": {"code": "INVALID_INPUT"}}
    print(json.dumps(response, separators=(",", ":")))


if __name__ == "__main__":
    main()
