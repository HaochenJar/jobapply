import json, sys
plan_path, idx = sys.argv[1], int(sys.argv[2])
d = json.load(open(plan_path))
s = d['steps'][idx]
js = s['js']
marker = "(async()=>{"
i = js.index(marker)
wrapped = js[:i] + "const r = await " + js[i:] + "; r"
print(wrapped)
