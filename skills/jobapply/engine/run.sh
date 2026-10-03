#!/bin/bash
# jobapply — daily loop entry point.  ./run.sh <discover|score|tailor|apply|inbox|dashboard|status>
set -euo pipefail
cd "$(dirname "$0")"

# Batch size for one invocation. NOT the daily cap: db.py claim() reads daily_target from
# profile.json and refuses every submit past it, so `./run.sh apply` twice does not buy a
# second day. Lower this to work in smaller chunks; raising it cannot exceed the real cap.
BATCH=${BATCH:-${TARGET:-100}}
MIN_SCORE=${MIN_SCORE:-40}     # score.py is honest, not generous; 40 is "worth the effort"

agent() { claude -p "Read $PWD/db.py --help first. $1"; }

case "${1:-status}" in

  discover)  python3 discover.py "${@:2}" ;;

  score)     python3 score.py --bulk ;;

  tailor)    agent "Run 'python3 $PWD/db.py queue --limit $BATCH
             --min-score $MIN_SCORE'. That query already applies the per-company caps and the
             tier mix, so take its rows AS GIVEN and never substitute your own SQL. For EACH
             one invoke the app-tailor subagent with that single uid. Report a table of
             uid/variant/sha1." ;;

  apply)     agent "Run 'python3 $PWD/db.py queue --limit $BATCH
             --min-score $MIN_SCORE' — that query already applies the per-company caps, so
             take its rows AS GIVEN and never substitute your own SQL. --limit is a batch
             size, NOT the daily cap: claim() enforces daily_target and will start refusing,
             which is the correct stopping signal. For EACH row invoke the app-filler
             subagent with that single uid, one at a time, >=60s apart. Stop early if
             claim() reports the daily cap, or if 3 in a row land in needs_human —
             something is broken. Report final status counts and every needs_human reason." ;;

  inbox)     agent "Use the inbox-watcher subagent to scan, classify, and write back." ;;
             # inbox stays an agent: Gmail classification genuinely needs judgement.

  dashboard) python3 dashboard.py ;;

  status)
    DB=$(ls ./*.db 2>/dev/null | head -1 || true)
    [ -n "$DB" ] || { echo "no database yet — ./run.sh discover"; exit 0; }
    sqlite3 "$DB" "
      select status, count(*) from applications group by 1 order by 2 desc;
      select 'submitted today', count(*) from applications
        where date(submitted_at)=date('now','localtime');
      select 'NEEDS HUMAN', count(*) from applications where status='needs_human';
      select 'FRAUD FLAG', count(*) from applications where status='fraud_flag';
      select 'role_tier '||coalesce(role_tier,'?'), count(*) from jobs
        where score>=$MIN_SCORE group by 1 order by 1;"
    ;;

  *) sed -n 2p "$0"; exit 1 ;;
esac
