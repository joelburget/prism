"""Scaling control for the documented unique-key aggregate workload only."""
import json
import sys

incremental = sys.argv[1] == 'incremental'
data = json.load(sys.stdin)['input']
tables = {t['name']: t['rows'] for t in data['database']}
left = [r[0] for r in tables['l']]
right = {i+1: list(r) for i,r in enumerate(tables['r'])}
assert left == list(range(len(left)))
assert [r[0] for r in right.values()] == left

def recompute():
    index = {r[0]: r[1] for r in right.values()}
    return sum(index[k] for k in left)

total = recompute()
revision = 0
replies = []
for command in data['commands']:
    if command['op'] == 'create':
        value = {'view': command['view'], 'revision': revision}
    elif command['op'] == 'apply':
        for change in command['changes']:
            assert change['op'] == 'update'
            if change['table'] == 'r':
                old = right[change['id']]
                assert old[0] == change['row'][0]
                total += change['row'][1] - old[1]
                right[change['id']] = change['row']
            else:
                assert change['table'] == 'u'
        revision += 1
        value = {'revision': revision}
    elif command['op'] == 'read':
        value = {'revision': revision, 'columns':['n','s'], 'rows':[[len(left),total if incremental else recompute()]]}
    else:
        raise ValueError('outside control workload')
    replies.append({'ok':True,'result':value})
print(json.dumps({'ok':True,'result':{'results':replies}}))
