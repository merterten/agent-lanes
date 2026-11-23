#!/usr/bin/env python3
"""Generate the "what runs where" diagram in English and Turkish, dark and light.

    python3 docs/img/gen_where.py      # writes docs/img/where-{en,tr}-{dark,light}.svg

One layout, two label sets, two colour themes: edit the labels here and regenerate, so all four files stay in sync.
Standalone SVG (no scripts, no external fonts or images) so GitHub can show it with <picture>/<img>.
"""
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent

LABELS = {
    "en": {
        "title": "What runs where",
        "phone": "Phone · iPad · Mac — Heeler",
        "phone2": "watch · reply in Chat · approve · Ctrl+C",
        "phone3": "push when any Claude / worker finishes",
        "push": "done → push",
        "net": "Tailscale",
        "net2": "(private network, SSH)",
        "pc": "Main PC · Windows + WSL",
        "chips": ("project-a", "project-b", "lanes"),
        "lead": "Claude Code (lead)",
        "lead2": "plans · splits · writes briefs",
        "lead3": "reads diffs · tests + mutants",
        "lane2": "worktree · ledger",
        "lane3": "accept · merge",
        "sandbox": "sandbox (bubblewrap): no keys · no git · own folder only",
        "done": "when done: REPORT.md + LANE_RESULT.json → Claude checks",
        "m2": "Second machine (optional)",
        "m22": "long tests · builds · grids — nice 19, rsync there and back",
        "desc": "Phone, iPad and Mac (Heeler) reach herdr on the main PC over Tailscale. On the PC, Claude Code leads in "
                "herdr workspaces and hands work through lane to OpenCode and Command Code workers in a sandbox; "
                "when a session or worker finishes, the phone gets a push. Long jobs can go to a second machine.",
    },
    "tr": {
        "title": "Neyi nerede kullanırsın",
        "phone": "Telefon · iPad · Mac — Heeler",
        "phone2": "izle · Chat'ten cevap yaz · onay ver · Ctrl+C",
        "phone3": "her Claude / işçi bitince push",
        "push": "bitti → push",
        "net": "Tailscale",
        "net2": "(özel ağ, SSH)",
        "pc": "Ana PC · Windows + WSL",
        "chips": ("proje-a", "proje-b", "lanes"),
        "lead": "Claude Code (lider)",
        "lead2": "planlar · böler · brief yazar",
        "lead3": "diff okur · test + mutant",
        "lane2": "worktree · defter",
        "lane3": "accept · merge",
        "sandbox": "sandbox (bubblewrap): anahtar yok · git yok · sadece kendi klasörü",
        "done": "bitince: REPORT.md + LANE_RESULT.json → Claude kontrol eder",
        "m2": "İkinci makine (isteğe bağlı)",
        "m22": "uzun test · derleme · grid — nice 19, rsync ile gider ve döner",
        "desc": "Telefon, iPad ve Mac (Heeler) Tailscale üzerinden ana PC'deki herdr'a bağlanır. PC'de lider Claude Code "
                "herdr workspace'lerinde çalışır ve işi lane ile sandbox içindeki OpenCode ve Command Code işçilerine "
                "verir; oturum ya da işçi bitince telefona push gelir. Uzun işler ikinci makineye gidebilir.",
    },
}

THEMES = {
    "dark": dict(bg="#0d1117", alt="#161b22", fg="#e6edf3", mut="#8b949e", line="#30363d", acc="#58a6ff", ok="#3fb950", warn="#d29922"),
    "light": dict(bg="#ffffff", alt="#f6f8fa", fg="#1f2328", mut="#59636e", line="#d1d9e0", acc="#0969da", ok="#1a7f37", warn="#9a6700"),
}


def svg(lang, theme):
    L, c = LABELS[lang], THEMES[theme]
    e = lambda s: escape(s)
    style = (
        f".bg{{fill:{c['alt']};stroke:{c['line']}}}.box{{fill:{c['bg']};stroke:{c['line']};stroke-width:1.5}}"
        f".acc{{stroke:{c['acc']}}}.ok{{stroke:{c['ok']}}}.sb{{fill:none;stroke:{c['warn']};stroke-dasharray:5 4}}"
        f".dash{{stroke-dasharray:5 4}}.chip{{fill:{c['alt']};stroke:{c['line']}}}"
        f".tt{{fill:{c['fg']};font:600 11px -apple-system,'Segoe UI',Helvetica,Arial,sans-serif}}"
        f".tm{{fill:{c['mut']};font:9.5px -apple-system,'Segoe UI',Helvetica,Arial,sans-serif}}"
        f".ln{{stroke:{c['mut']};stroke-width:1.5;fill:none}}.lacc{{stroke:{c['acc']}}}.lok{{stroke:{c['ok']}}}"
        f".ah{{fill:{c['acc']}}}.ahok{{fill:{c['ok']}}}"
    )

    def box(x, y, w, h, cls, a, b="", d=""):
        out = f'<rect class="box {cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="8"/><text class="tt" x="{x + 10}" y="{y + 18}">{e(a)}</text>'
        if b:
            out += f'<text class="tm" x="{x + 10}" y="{y + 33}">{e(b)}</text>'
        if d:
            out += f'<text class="tm" x="{x + 10}" y="{y + 47}">{e(d)}</text>'
        return out

    def down(x, y1, y2):
        return f'<line class="ln lacc" x1="{x}" y1="{y1}" x2="{x}" y2="{y2 - 6}"/><polygon class="ah" points="{x - 4},{y2 - 7} {x + 4},{y2 - 7} {x},{y2}"/>'

    def up(x, y1, y2):
        return f'<line class="ln lok" x1="{x}" y1="{y1}" x2="{x}" y2="{y2 + 6}"/><polygon class="ahok" points="{x - 4},{y2 + 7} {x + 4},{y2 + 7} {x},{y2}"/>'

    chips = "".join(
        f'<rect class="chip" x="{x}" y="143" width="72" height="20" rx="10"/><text class="tm" x="{x + 36}" y="157" text-anchor="middle">{e(t)}</text>'
        for x, t in zip((78, 158, 238), L["chips"]))
    body = "".join([
        f'<rect class="bg" x="0.5" y="0.5" width="359" height="469" rx="12"/>',
        box(10, 10, 340, 54, "ok", L["phone"], L["phone2"], L["phone3"]),
        up(120, 108, 64), f'<text class="tm" x="128" y="90">{e(L["push"])}</text>',
        down(240, 64, 108), f'<text class="tm" x="248" y="84">{e(L["net"])}</text><text class="tm" x="248" y="97">{e(L["net2"])}</text>',
        f'<rect class="box acc" x="10" y="108" width="340" height="292" rx="10"/><text class="tt" x="22" y="127">{e(L["pc"])}</text>',
        f'<rect class="box" x="22" y="136" width="316" height="34" rx="6"/><text class="tt" x="32" y="157">herdr</text>', chips,
        box(22, 184, 150, 58, "acc", L["lead"], L["lead2"], L["lead3"]),
        box(188, 184, 150, 58, "", "lane", L["lane2"], L["lane3"]),
        '<line class="ln lacc" x1="172" y1="213" x2="182" y2="213"/><polygon class="ah" points="181,209 188,213 181,217"/>',
        down(263, 242, 268),
        f'<rect class="box sb" x="22" y="268" width="316" height="120" rx="8"/><text class="tm" x="32" y="285">{e(L["sandbox"])}</text>',
        box(32, 296, 92, 52, "ok", "oc-worker-01", "OpenCode", "DeepSeek"),
        box(134, 296, 92, 52, "ok", "oc-worker-02", "OpenCode", "DeepSeek"),
        box(236, 296, 92, 52, "ok", "cc-worker-03", "Command Code", "DeepSeek"),
        f'<text class="tm" x="32" y="374">{e(L["done"])}</text>',
        '<line class="ln dash" x1="180" y1="400" x2="180" y2="420"/>',
        f'<rect class="box dash" x="10" y="420" width="340" height="42" rx="8"/><text class="tt" x="22" y="438">{e(L["m2"])}</text>',
        f'<text class="tm" x="22" y="453">{e(L["m22"])}</text>',
    ])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 470" width="360" height="470" role="img" '
            f'aria-labelledby="t d"><title id="t">{e(L["title"])}</title><desc id="d">{e(L["desc"])}</desc>'
            f'<style>{style}</style>{body}</svg>\n')


def main():
    for lang in LABELS:
        for theme in THEMES:
            p = OUT / f"where-{lang}-{theme}.svg"
            p.write_text(svg(lang, theme), encoding="utf-8")
            print(p.relative_to(OUT.parent.parent))


if __name__ == "__main__":
    main()
