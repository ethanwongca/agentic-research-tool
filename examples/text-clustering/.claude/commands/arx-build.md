---
description: Implement the next approved box exactly as specified, verify it, and stop. Never redesigns.
---
1. Run `arx next` and do exactly what it says. If the next stage is DRAFT, READY or BLOCKED, stop and hand
   back to the researcher.
2. For an APPROVED stage: run `arx scaffold <stage>`, then implement the generated module literally:
   follow the approach, constraints and `params`. If the spec doesn't settle a choice, STOP, `arx ask` a
   neutral question, and ask the researcher.
3. Run `arx verify <stage>`. If it fails, fix **implementation bugs** only. Never change the method to make
   the checks pass, and never touch the checks.
4. If the stage becomes BLOCKED (too many failed attempts), or you believe the specified approach itself
   can't pass the checks, stop and report facts only: which checks fail, the error output, the data you
   observed, and what you changed in each attempt. Don't propose a fix to the approach. The researcher
   will add or revise a box on the board.
5. When verified, the stage is locked. Report the check results and stop.
