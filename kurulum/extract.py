#!/usr/bin/env python3
"""KURULUM.md içindeki ```dil file=~/yol blokları -> scripts/home/yol (tek kaynak: KURULUM.md)."""
import re, sys, pathlib, os
here = pathlib.Path(__file__).resolve().parent
src = (here / "KURULUM.md").read_text(encoding="utf-8")
out = here / "scripts" / "home"
n = 0
for m in re.finditer(r"^```\S*\s+file=~/(\S+)\n(.*?)^```$", src, re.S | re.M):
    p = out / m.group(1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(m.group(2), encoding="utf-8")
    if m.group(2).startswith("#!"):
        p.chmod(0o755)
    n += 1
    print(p.relative_to(here))
print(n, "dosya")
