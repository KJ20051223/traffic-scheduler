import signal
import sys
import time
from vps_toolkit.core import Context, Error
from vps_toolkit.traffic import tick


def stop(signum, frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
while True:
    try:
        tick(Context())
    except (Error, OSError, ValueError) as error:
        print('定时任务失败: ' + str(error), file=sys.stderr, flush=True)
    time.sleep(max(1, 60 - time.time() % 60))
