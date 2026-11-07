"""생성한 문제를 눈으로 검수하는 HTML 페이지를 만든다. OpenAI를 부르지 않는다.

    .venv/bin/python -m eval.review_page "eval/out/자료.u3.ai02.csv=Overriding (5,799자)" ...

브라우저로 eval/out/검수.html을 열고 문제마다 정상/경미/치명을 누른다. 표시는 브라우저에 저장되고,
"라벨 CSV 저장"으로 내려받는다. 강의자료 문장이 들어 있어 eval/out에만 두고 올리지 않는다.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

from eval.run_ai01 import OUT

PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>문제 검수</title>
<style>
:root { --bg:#f6f6f4; --card:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e2dd; --accent:#2f5bd3;
  --ok:#1f8a4c; --minor:#b7791f; --fatal:#c53030; --okbg:#e6f4ec; --minorbg:#fbf1dc; --fatalbg:#fbe5e5; --hl:#eaf0ff; }
@media (prefers-color-scheme: dark) { :root { --bg:#151514; --card:#1f1f1d; --text:#ecebe6; --muted:#9b9a93; --line:#33322f;
  --accent:#7d9cf0; --okbg:#173325; --minorbg:#352a12; --fatalbg:#3a1c1c; --hl:#1d2740; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.6 -apple-system, "Apple SD Gothic Neo", "Pretendard", sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line); padding:12px 16px; }
.wrap { max-width:860px; margin:0 auto; }
h1 { font-size:18px; margin:0 0 8px; }
.stats { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; font-size:14px; color:var(--muted); }
.stats b { color:var(--text); }
.bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin:8px 0; display:flex; }
.bar span { display:block; height:100%; }
.controls { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
select, button { font:inherit; font-size:14px; padding:6px 10px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--text); }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
main { padding:16px; }
.guide { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 16px; font-size:14px; color:var(--muted); margin-bottom:16px; }
.guide b { color:var(--text); }
h2 { font-size:16px; margin:28px 0 10px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:12px; }
.card.ok { border-left:4px solid var(--ok); } .card.minor { border-left:4px solid var(--minor); } .card.fatal { border-left:4px solid var(--fatal); }
.meta { display:flex; gap:8px; align-items:center; font-size:13px; color:var(--muted); margin-bottom:6px; }
.type { font-weight:600; color:var(--accent); }
.stem { font-size:16px; font-weight:600; margin:0 0 8px; }
ol.choices { margin:0 0 8px; padding-left:22px; }
ol.choices li.right { background:var(--hl); border-radius:6px; font-weight:600; }
.answer { margin:0 0 8px; } .answer b { color:var(--accent); }
.expl { margin:0 0 8px; }
.evidence { font-size:13px; color:var(--muted); border-left:3px solid var(--line); padding:2px 0 2px 10px; margin:0 0 8px; white-space:pre-wrap; }
.warn { font-size:12px; color:var(--minor); margin-left:6px; }
.tags { font-size:13px; color:var(--muted); }
.label { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; align-items:center; }
.label button[data-v="ok"].on { background:var(--okbg); border-color:var(--ok); color:var(--ok); }
.label button[data-v="minor"].on { background:var(--minorbg); border-color:var(--minor); color:var(--minor); }
.label button[data-v="fatal"].on { background:var(--fatalbg); border-color:var(--fatal); color:var(--fatal); }
.label input { flex:1; min-width:160px; font:inherit; font-size:14px; padding:6px 10px; border-radius:8px; border:1px solid var(--line); background:var(--bg); color:var(--text); }
</style>
</head>
<body>
<header><div class="wrap">
  <h1>AI 문제 검수</h1>
  <div class="stats" id="stats"></div>
  <div class="bar" id="bar"></div>
  <div class="controls">
    <select id="unit"></select>
    <select id="type"><option value="">모든 유형</option><option>OX</option><option>MC</option><option>SHORT</option></select>
    <select id="state"><option value="">전체</option><option value="todo">아직 안 본 것</option><option value="fatal">치명만</option><option value="minor">경미만</option></select>
    <button class="primary" id="save">라벨 CSV 저장</button>
  </div>
</div></header>
<main class="wrap">
  <div class="guide">
    <b>치명</b>: 정답이 틀림, 문제가 성립하지 않음, 자료에 없는 내용, 근거 쪽이 틀림 ·
    <b>경미</b>: 답은 맞지만 어색함, 너무 쉬움, 다른 문제와 거의 같음, 해설이 부족함 ·
    <b>정상</b>: 그대로 내도 됨. 표시는 이 브라우저에 자동 저장돼요.
  </div>
  <div id="list"></div>
</main>
<script>
const DATA = __DATA__;
const KEY = "review-labels-v2";
let labels = {};
try { labels = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(labels)); } catch (e) {} };
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const NAMES = { OX: "O/X", MC: "객관식", SHORT: "단답" };
const LABELS = { ok: "정상", minor: "경미", fatal: "치명" };

const unitSel = document.getElementById("unit");
unitSel.innerHTML = '<option value="">모든 단원</option>' + DATA.units.map((u, i) => `<option value="${i}">${esc(u.name)} (${u.questions.length})</option>`).join("");

function card(q, id) {
  const l = labels[id] || {};
  let body = "";
  if (q.type === "MC") {
    body = '<ol class="choices">' + q.choices.map((c, i) => `<li class="${String(i + 1) === q.answer ? "right" : ""}">${esc(c)}</li>`).join("") + "</ol>";
  } else if (q.type === "OX") {
    body = `<p class="answer">정답 <b>${esc(q.answer)}</b></p>`;
  } else {
    body = `<p class="answer">정답 <b>${esc(q.answer)}</b>${q.accepted.length ? " · 인정 답: " + esc(q.accepted.join(", ")) : ""}</p>`;
  }
  const warn = q.quoteRepeat > 1 ? `<span class="warn">같은 근거 문장 ${q.quoteRepeat}문제</span>` : "";
  return `<div class="card ${l.v || ""}" data-id="${id}">
    <div class="meta"><span class="type">${NAMES[q.type] || q.type}</span><span>${q.page}쪽 근거</span>${warn}</div>
    <p class="stem">${esc(q.stem)}</p>${body}
    <p class="expl">${esc(q.explanation)}</p>
    <p class="evidence">${esc(q.quote)}</p>
    <div class="tags">개념: ${esc(q.tags)}</div>
    <div class="label">
      ${Object.entries(LABELS).map(([v, t]) => `<button data-v="${v}" class="${l.v === v ? "on" : ""}">${t}</button>`).join("")}
      <input placeholder="메모 (치명·경미일 때 이유)" value="${esc(l.memo || "")}">
    </div></div>`;
}

function render() {
  const u = unitSel.value, t = document.getElementById("type").value, s = document.getElementById("state").value;
  let html = "";
  DATA.units.forEach((unit, ui) => {
    if (u !== "" && Number(u) !== ui) return;
    const cards = unit.questions.map((q, qi) => [q, `${unit.key}#${qi}`]).filter(([q, id]) => {
      const v = (labels[id] || {}).v;
      return (!t || q.type === t) && (!s || (s === "todo" ? !v : v === s));
    });
    if (!cards.length) return;
    html += `<h2>${esc(unit.name)}</h2>` + cards.map(([q, id]) => card(q, id)).join("");
  });
  document.getElementById("list").innerHTML = html || "<p>조건에 맞는 문제가 없어요.</p>";
  stats();
}

function stats() {
  const all = DATA.units.flatMap(u => u.questions.map((_, i) => `${u.key}#${i}`));
  const c = { ok: 0, minor: 0, fatal: 0 };
  all.forEach(id => { const v = (labels[id] || {}).v; if (v) c[v]++; });
  const done = c.ok + c.minor + c.fatal;
  const rate = done ? (c.fatal / done * 100).toFixed(1) : "-";
  document.getElementById("stats").innerHTML =
    `<span>검수 <b>${done}</b> / ${all.length}</span><span>정상 <b>${c.ok}</b></span><span>경미 <b>${c.minor}</b></span>` +
    `<span>치명 <b>${c.fatal}</b></span><span>치명 오류율 <b>${rate}${done ? "%" : ""}</b> (기준 5% 이하, 목표 3%)</span>`;
  const w = n => (n / all.length * 100) + "%";
  document.getElementById("bar").innerHTML =
    `<span style="width:${w(c.ok)};background:var(--ok)"></span><span style="width:${w(c.minor)};background:var(--minor)"></span><span style="width:${w(c.fatal)};background:var(--fatal)"></span>`;
}

document.getElementById("list").addEventListener("click", e => {
  const b = e.target.closest("button[data-v]"); if (!b) return;
  const cardEl = b.closest(".card"), id = cardEl.dataset.id, l = labels[id] || {};
  l.v = l.v === b.dataset.v ? undefined : b.dataset.v;
  labels[id] = l; save();
  cardEl.className = "card " + (l.v || "");
  cardEl.querySelectorAll("button[data-v]").forEach(x => x.classList.toggle("on", x.dataset.v === l.v));
  stats();
});
document.getElementById("list").addEventListener("input", e => {
  if (e.target.tagName !== "INPUT") return;
  const id = e.target.closest(".card").dataset.id;
  labels[id] = { ...(labels[id] || {}), memo: e.target.value }; save();
});
["unit", "type", "state"].forEach(x => document.getElementById(x).addEventListener("change", render));

document.getElementById("save").addEventListener("click", () => {
  const rows = [["단원", "유형", "지문", "정답", "근거쪽", "라벨", "메모"]];
  DATA.units.forEach(u => u.questions.forEach((q, i) => {
    const l = labels[`${u.key}#${i}`] || {};
    rows.push([u.name, q.type, q.stem, q.answer, q.page, LABELS[l.v] || "", l.memo || ""]);
  }));
  const csv = "\\ufeff" + rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(",")).join("\\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "문제_검수_라벨.csv"; a.click();
});
render();
</script>
</body>
</html>
"""


def load(arg: str) -> dict:
    path, _, name = arg.partition("=")
    rows = list(csv.DictReader(Path(path).open(encoding="utf-8-sig")))
    repeats = Counter(r["근거문장"] for r in rows)
    questions = [
        {
            "type": r["유형"],
            "stem": r["지문"],
            "choices": r["보기"].split(" / ") if r["보기"] else [],
            "answer": r["정답"],
            "accepted": r["정답목록"].split(" / ") if r["정답목록"] else [],
            "explanation": r["해설"],
            "page": r["근거쪽"],
            "quote": r["근거문장"],
            "quoteRepeat": repeats[r["근거문장"]],
            "tags": r["개념태그"],
        }
        for r in rows
    ]
    # 수정 전·후 파일 이름이 같아서 경로로 구분한다
    return {"key": path, "name": name or Path(path).name, "questions": questions}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    data = {"units": [load(a) for a in sys.argv[1:]]}
    out = OUT / "검수.html"
    # </script>가 데이터 안에 있어도 스크립트가 끊기지 않게 한다.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(PAGE.replace("__DATA__", payload), encoding="utf-8")
    print(f"{out} ({sum(len(u['questions']) for u in data['units'])}문제)")
