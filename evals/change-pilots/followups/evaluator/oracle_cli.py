#!/usr/bin/env python3
"""Wire smoke-test adapter for private fixture models; NOT a qualified solution."""
import json
import sys
from query_model import evaluate as query
from workflow_model import evaluate as workflow

request=json.load(sys.stdin)
function={'query-null':query,'workflow-recovery':workflow}[request['task']]
print(json.dumps(function(request['input'])))
