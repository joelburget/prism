"""One-request JSON adapter; each request owns a fresh simulator."""
import json
import sys
from workflow import DomainError, Simulator, object_fields, parse_workflow, require


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Reject duplicate JSON members instead of silently keeping the last one."""
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def main() -> None:
    try:
        request = json.load(sys.stdin, object_pairs_hook=unique_object)
        object_fields(request, {"protocol_version", "task", "input"})
        require(type(request["protocol_version"]) is int and request["protocol_version"] == 1)
        require(request["task"] == "workflow-recovery")
        result = Simulator(parse_workflow(request["input"])).run()
        response = {"ok": True, "result": result}
    except DomainError as error:
        response = {"ok": False, "error": {"code": error.code}}
    except (ValueError, KeyError, TypeError):
        response = {"ok": False, "error": {"code": "INVALID_INPUT"}}
    print(json.dumps(response, separators=(",", ":")))


if __name__ == "__main__":
    main()
