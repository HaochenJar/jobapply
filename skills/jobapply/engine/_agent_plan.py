import sqlite3, json, sys, adapters

uid = sys.argv[1]
url_override = sys.argv[2] if len(sys.argv) > 2 else None
platform_override = sys.argv[3] if len(sys.argv) > 3 else None

c = sqlite3.connect('jobapply.db').cursor()
c.execute('select * from jobs where uid=?', (uid,))
r = c.fetchone()
row = dict(zip([d[0] for d in c.description], r))
resume = 'applications/%s/resume.pdf' % uid.replace(':', '_')
row['overrides'] = {'resume_pdf': resume}
if platform_override:
    row['platform'] = platform_override
url = url_override or row['apply_url']
p = adapters.plan(url, job=row)
print(json.dumps(p, indent=1))
