#!/bin/bash
WD=/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861/powe-152-4bed59acb45b/workdir
PY=$WD/datus-runtime/.venv/bin/python
SC=$WD/.scoring-venv/bin/python
cd $WD/experiment
# wait for D23x3 completion marker
while ! grep -q "D23X3 DONE" $WD/r3/d23x3.log; do sleep 30; done
echo "=== D23x3 finished $(date +%H:%M:%S); scoring any missing reps"
for arm in p2 s3v1 s3v2; do for rep in 1 2 3; do
  if [ ! -f $WD/r3/reports/$arm-d23-rep$rep.json ] && [ -d $WD/r3/runs/$arm/d23-rep$rep ]; then
    $SC r3_drive.py score --arm $arm --qset d23 --rep $rep 2>/dev/null | tail -1
  fi
done; done
echo "=== selection + freeze"
$SC r3_select.py freeze --candidates p2,s3v1,s3v2
CHAMP=$($SC -c "import json;print(json.load(open('$WD/r3/freeze/champion-r3.json'))['champion'])")
echo "=== champion: $CHAMP"
echo "=== R x3 + H2 x3 for $CHAMP $(date +%H:%M:%S)"
set -a; . /home/rongfeng.frf/workspace/.env; set +a
export DB_PASSWORD='TOu)Fwb7DOq@aATFoaj@d^'
for rep in 1 2 3; do
  $PY r3_drive.py run --arm $CHAMP --qset r --rep $rep || echo "BATCH FAILED r $rep"
  $SC r3_drive.py score --arm $CHAMP --qset r --rep $rep 2>/dev/null | tail -1
done
for rep in 1 2 3; do
  $PY r3_drive.py run --arm $CHAMP --qset h2 --rep $rep || echo "BATCH FAILED h2 $rep"
  $SC r3_drive.py score --arm $CHAMP --qset h2 --rep $rep 2>/dev/null | tail -1
done
echo "=== FINAL EVAL DONE $(date +%H:%M:%S)"
$SC r3_select.py final --arm $CHAMP
$SC r3_drive.py cost 2>/dev/null | tail -1
