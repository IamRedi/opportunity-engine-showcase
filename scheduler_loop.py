"""Proces i dedikuar i schedulit (Faza 7A) — i pavarur nga web serveri.

Nisja:    .venv/bin/python scheduler_loop.py            # loop çdo 60s
         .venv/bin/python scheduler_loop.py --interval 120
Një herë: .venv/bin/python scheduler_loop.py --once      # për cron/systemd
Ndalja:   Ctrl+C (SIGTERM) — run-i në proces përfundon, next_run_at
          është tashmë i avancuar (CAS) → asnjë ekzekutim i dyfishtë.

Web app-i punon normalisht edhe pa këtë proces; scheduli thjesht nuk
ekzekutohet derisa të niset përsëri (ose përmes cron me --once).
"""
import argparse
import time

import repository as repo
import scheduler

repo.init()  # idempotent — siguron tabelat edhe për --once në DB të re


def main(interval: int, once: bool) -> None:
    if once:
        res = scheduler.run_due()
        print(f"scheduler tick: due={res['due']} ran={res['ran']}")
        return
    print(f"scheduler loop nisur (interval {interval}s) — Ctrl+C për ndalje")
    while True:
        try:
            res = scheduler.run_due()
            if res["due"]:
                print(f"tick: due={res['due']} ran={res['ran']}")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # loop-i kurrë s'vdes nga një gabim run-i
            print("tick error (fail-soft):", e)
        time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Opportunity Engine scheduler")
    ap.add_argument("--interval", type=int, default=60, help="sekonda midis kontrolleve")
    ap.add_argument("--once", action="store_true", help="ekzekuto një tick dhe dal")
    args = ap.parse_args()
    try:
        main(args.interval, args.once)
    except KeyboardInterrupt:
        print("scheduler ndaluar")
