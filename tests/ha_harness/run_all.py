"""Run every real-Home-Assistant check, each in its own process with a time limit, and print one line per script.

    python tests/ha_harness/run_all.py              # all of them
    python tests/ha_harness/run_all.py e2e_soak     # only scripts whose name contains this

Exits with 1 if any script fails or runs out of time (needs Home Assistant installed: see README.md here)."""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
LIMIT_S = int(os.environ.get("HARNESS_TIMEOUT_S", "600"))
COVER = ["-m", "coverage", "run", "-p"] if os.environ.get("HARNESS_COVERAGE") else []   # see the coverage job in tests.yml


def main() -> int:
    want = sys.argv[1:]
    scripts = [HERE / "check_imports.py", HERE / "configure_flows.py", HERE / "check_form_schemas.py"] + sorted(HERE.glob("e2e_*.py"))
    if want:
        scripts = [s for s in scripts if any(w in s.stem for w in want)]
    env = {**os.environ, "SOAK_CYCLES": os.environ.get("SOAK_CYCLES", "300"), "PYTHONUNBUFFERED": "1"}
    failed = []
    for s in scripts:
        t0 = time.monotonic()
        try:
            r = subprocess.run([sys.executable, *COVER, str(s)], cwd=HERE.parent.parent, env=env, capture_output=True, text=True, timeout=LIMIT_S)
            ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired as err:
            ok, out = False, f"TIMED OUT after {LIMIT_S} s\n" + ((err.stdout or b"").decode(errors="replace") if isinstance(err.stdout, bytes) else (err.stdout or ""))
        last = out.splitlines()[-1] if out else ""
        print(f"{'ok  ' if ok else 'FAIL'} {s.stem:<28} {time.monotonic() - t0:6.1f} s  {last[:100]}", flush=True)
        if not ok:
            failed.append(s.stem)
            print("\n".join("     | " + line for line in out.splitlines()[-25:]), flush=True)
    print(f"\n{len(scripts) - len(failed)} of {len(scripts)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
