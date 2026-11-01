"""Record that the EC CPU has been halted in this power cycle.

The halt (DBGR 0x27 <- 0x81) is a CPU reset. Sending it again later in a
session can let the EC firmware run for a moment: on an IT5261 one of its
data sectors was erased right after such a second reset. So the CPU is
halted once, by `enter` (or --halt), which writes this file, and the
commands that follow read it instead of halting again. `safe` and `lines`
delete it: they come before unplugging AC.
"""

import json
import os
import time

PATH_ENV = 'ITEPP_SESSION'
DEFAULT_PATH = os.path.join(os.path.expanduser('~'), '.cache', 'ite-pp-isp', 'session.json')


class Session:
    def __init__(self, sim=False):
        path = os.environ.get(PATH_ENV) or DEFAULT_PATH
        # a simulated run must never stand in for the real EC, or the reverse
        self.path = path + '.sim' if sim else path

    def save(self, halts):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        with open(self.path, 'w') as f:
            json.dump({'halted_at': time.time(), 'halts': halts}, f)

    def load(self):
        """The saved state (dict), or None if the CPU isn't known to be halted."""
        try:
            with open(self.path) as f:
                state = json.load(f)
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) and 'halted_at' in state else None

    def clear(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    @staticmethod
    def describe(state):
        t = state['halted_at']
        minutes = max(0, int(time.time() - t) // 60)
        return f'at {time.strftime("%H:%M", time.localtime(t))} ({minutes} min ago)'
