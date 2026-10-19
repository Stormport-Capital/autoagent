"""Step 5: one-page HTML summary - both summary tables and average net R by
entry day for each strategy. Static numbers at the workbook's default costs
(engine defaults: 5 bps per side, 10%/yr borrow / 360); the workbook is the
live version.

    write_html(path, trade_rows, meta)
"""

from __future__ import annotations

import html
import json
import statistics

import report
import trades as T

COSTS = {"slip_pct": 0.05, "borrow_pct": 10.0, "day_count": 360}


def _stats(sel):
    closed = [r for r in sel if r["status"] == "CLOSED"]
    nr = [T.net_r(r, COSTS["slip_pct"], COSTS["borrow_pct"], COSTS["day_count"]) for r in closed]
    streak = cur = 0
    for r, x in sorted(zip(closed, nr), key=lambda p: p[0]["entry_time"]):
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    return {
        "closed": len(closed), "open": sum(r["status"] == "OPEN" for r in sel),
        "win": (sum(x > 0 for x in nr) / len(nr)) if nr else None,
        "avg_gross": statistics.mean(r["gross_r"] for r in closed) if closed else None,
        "avg_net": statistics.mean(nr) if nr else None, "tot_net": sum(nr),
        "hold": statistics.mean(r["holding_days"] for r in closed) if closed else None,
        "streak": streak if closed else None,
        "worst": min(nr) if nr else None, "best": max(nr) if nr else None,
    }


def _fmt(v, kind):
    if v is None:
        return "–"
    return {"pct": f"{v:.0%}", "r": f"{v:+.2f}", "d": f"{v:.1f}", "n": f"{v:,}"}[kind]


def _table(rows, event_type):
    out = ['<table><thead><tr><th>Strategy</th><th>TF</th><th>Dir</th><th>Closed</th><th>Open</th>'
           '<th>Win %</th><th>Avg gross R</th><th>Avg net R</th><th>Total net R</th><th>Avg hold (d)</th>'
           '<th>Max loss streak</th><th>Worst R</th><th>Best R</th><th>Note</th></tr></thead><tbody>']
    for s in report.STRATEGIES:
        for tf in report.TIMEFRAMES:
            for d in report.DIRECTIONS:
                if s in report.SHORT_ONLY and d == "long":
                    continue
                st = _stats([r for r in rows if r["event_type"] == event_type and r["strategy"] == s
                             and r["timeframe"] == tf and r["direction"] == d])
                note = "small sample" if st["closed"] < report.SMALL_SAMPLE else ""
                cells = [html.escape(s), tf, d, _fmt(st["closed"], "n"), _fmt(st["open"], "n"),
                         _fmt(st["win"], "pct"), _fmt(st["avg_gross"], "r"), _fmt(st["avg_net"], "r"),
                         _fmt(st["tot_net"] if st["closed"] else None, "r"), _fmt(st["hold"], "d"),
                         _fmt(st["streak"], "n"), _fmt(st["worst"], "r"), _fmt(st["best"], "r"), note]
                out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def by_day(rows):
    data = {}
    for et in report.EVENT_TYPES:
        for s in report.STRATEGIES:
            for d in report.DIRECTIONS:
                if s in report.SHORT_ONLY and d == "long":
                    continue
                series = {}
                for tf in report.TIMEFRAMES:
                    pts = []
                    for day in range(26):
                        sel = [r for r in rows if r["event_type"] == et and r["strategy"] == s and r["timeframe"] == tf
                               and r["direction"] == d and r["entry_day"] == day and r["status"] == "CLOSED"]
                        nr = [T.net_r(r, COSTS["slip_pct"], COSTS["borrow_pct"], COSTS["day_count"]) for r in sel]
                        pts.append([day, statistics.mean(nr) if nr else None, len(nr)])
                    series[tf] = pts
                data.setdefault(et, []).append({"title": f"{s} · {d}", "series": series})
    return data


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tranche Event Backtest</title>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--base:#c3c2b7;--ring:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--base:#383835;--ring:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--base:#383835;--ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}p.meta{color:var(--ink2);margin:0 0 16px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:12px}
th,td{padding:4px 6px;text-align:right;border-bottom:1px solid var(--grid);white-space:nowrap}
th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}th{color:var(--ink2);font-weight:600}
td:last-child{color:var(--muted)}
.filters{display:flex;gap:8px;margin:8px 0 12px}.filters button{font:inherit;padding:4px 12px;border-radius:6px;
border:1px solid var(--ring);background:var(--surface);color:var(--ink);cursor:pointer}
.filters button[aria-pressed="true"]{border-color:var(--ink2);font-weight:600}
.legend{display:flex;gap:16px;color:var(--ink2);font-size:13px;margin-bottom:8px}
.legend span::before{content:"";display:inline-block;width:14px;height:2px;margin-right:6px;vertical-align:middle;background:var(--c)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.panel h3{font-size:13px;margin:0 0 4px;color:var(--ink)}svg{display:block;width:100%;height:auto}
.tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--ring);border-radius:6px;
padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none}
.tip b{font-variant-numeric:tabular-nums}details{margin-top:6px;color:var(--ink2);font-size:12px}
</style></head><body><main>
<h1>Tranche-dashboard strategies on +100% days</h1>
<p class="meta" id="meta"></p>
<h2>Summary · INTRADAY_100</h2><div class="card">__T1__</div>
<h2>Summary · CLOSE_100</h2><div class="card">__T2__</div>
<h2>Average net R by entry day</h2>
<div class="filters" role="group" aria-label="Event type"><button data-et="INTRADAY_100" aria-pressed="true">INTRADAY_100</button>
<button data-et="CLOSE_100" aria-pressed="false">CLOSE_100</button></div>
<div class="legend"><span style="--c:var(--s1)">1h</span><span style="--c:var(--s2)">15m</span><span style="--c:var(--s3)">5m</span></div>
<div class="grid" id="grid"></div><div class="tip" id="tip"></div>
</main><script>
const DATA=__DATA__, META=__META__;
document.getElementById("meta").textContent=META;
const TF=["1h","15m","5m"], COL={"1h":"var(--s1)","15m":"var(--s2)","5m":"var(--s3)"};
const tip=document.getElementById("tip");
function el(n,a,p){const e=document.createElementNS("http://www.w3.org/2000/svg",n);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e}
function draw(et){
 const grid=document.getElementById("grid");grid.textContent="";
 for(const pnl of DATA[et]){
  const card=document.createElement("div");card.className="card panel";
  const h=document.createElement("h3");h.textContent=pnl.title;card.appendChild(h);
  const W=340,H=200,L=40,R=30,T=8,B=24;
  const ys=[];for(const tf of TF)for(const p of pnl.series[tf])if(p[1]!==null)ys.push(p[1]);
  let lo=Math.min(-1,...ys),hi=Math.max(1,...ys);const pad=(hi-lo)*.08;lo-=pad;hi+=pad;
  const x=d=>L+d*(W-L-R)/25, y=v=>T+(hi-v)*(H-T-B)/(hi-lo);
  const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":pnl.title+" average net R by entry day"},card);
  for(const v of [lo+pad,0,hi-pad]){if(v!==0&&Math.abs(y(v)-y(0))<12)continue;el("line",{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:v===0?"var(--base)":"var(--grid)","stroke-width":1},svg);
   const t=el("text",{x:L-4,y:y(v)+3,"text-anchor":"end","font-size":10,fill:"var(--muted)"},svg);t.textContent=(v>0?"+":"")+v.toFixed(1)}
  for(const d of [0,5,10,15,20,25]){const t=el("text",{x:x(d),y:H-8,"text-anchor":"middle","font-size":10,fill:"var(--muted)"},svg);t.textContent=d}
  for(const tf of TF){
   let path="",pen=false;
   for(const p of pnl.series[tf]){if(p[1]===null){pen=false;continue}path+=(pen?"L":"M")+x(p[0])+" "+y(p[1]);pen=true}
   if(path)el("path",{d:path,fill:"none",stroke:COL[tf],"stroke-width":2,"stroke-linejoin":"round"},svg);
   for(const p of pnl.series[tf])if(p[1]!==null)el("circle",{cx:x(p[0]),cy:y(p[1]),r:3,fill:COL[tf],stroke:"var(--surface)","stroke-width":1.5},svg);
   const last=[...pnl.series[tf]].reverse().find(p=>p[1]!==null);
   if(last){const t=el("text",{x:x(last[0])+5,y:y(last[1])+3,"font-size":10,fill:"var(--ink2)"},svg);t.textContent=tf}
  }
  const hair=el("line",{y1:T,y2:H-B,stroke:"var(--base)","stroke-width":1,visibility:"hidden"},svg);
  const hit=el("rect",{x:L,y:T,width:W-L-R,height:H-T-B,fill:"transparent",tabindex:0},svg);
  const show=(d,cx,cy)=>{hair.setAttribute("x1",x(d));hair.setAttribute("x2",x(d));hair.setAttribute("visibility","visible");
   tip.textContent="";const hd=document.createElement("div");hd.textContent="Entry day "+d;tip.appendChild(hd);
   for(const tf of TF){const p=pnl.series[tf][d];const row=document.createElement("div");const b=document.createElement("b");
    b.textContent=p[1]===null?"–":(p[1]>0?"+":"")+p[1].toFixed(2)+"R";row.appendChild(b);
    row.appendChild(document.createTextNode("  "+tf+" · "+p[2]+" closed"));tip.appendChild(row)}
   tip.style.display="block";tip.style.left=(cx+12)+"px";tip.style.top=(cy+12)+"px"};
  hit.addEventListener("pointermove",ev=>{const r=svg.getBoundingClientRect();const sx=(ev.clientX-r.left)*W/r.width;
   const d=Math.max(0,Math.min(25,Math.round((sx-L)*25/(W-L-R))));show(d,ev.clientX,ev.clientY)});
  hit.addEventListener("pointerleave",()=>{tip.style.display="none";hair.setAttribute("visibility","hidden")});
  hit.addEventListener("focus",()=>{const r=svg.getBoundingClientRect();show(0,r.left,r.top)});
  hit.addEventListener("blur",()=>{tip.style.display="none";hair.setAttribute("visibility","hidden")});
  const det=document.createElement("details");const sm=document.createElement("summary");sm.textContent="Table";det.appendChild(sm);
  const tb=document.createElement("table");const hr=tb.insertRow();for(const c of ["Day","1h R (n)","15m R (n)","5m R (n)"]){const th=document.createElement("th");th.textContent=c;hr.appendChild(th)}
  for(let d=0;d<26;d++){const tr=tb.insertRow();tr.insertCell().textContent=d;
   for(const tf of TF){const p=pnl.series[tf][d];tr.insertCell().textContent=p[1]===null?"–":p[1].toFixed(2)+" ("+p[2]+")"}}
  det.appendChild(tb);card.appendChild(det);grid.appendChild(card)}
}
for(const b of document.querySelectorAll(".filters button"))b.addEventListener("click",()=>{
 for(const o of document.querySelectorAll(".filters button"))o.setAttribute("aria-pressed",o===b);draw(b.dataset.et)});
draw("INTRADAY_100");
</script></body></html>"""


def write_html(path: str, rows: list[dict], meta: str) -> None:
    page = (PAGE.replace("__T1__", _table(rows, "INTRADAY_100"))
                .replace("__T2__", _table(rows, "CLOSE_100"))
                .replace("__DATA__", json.dumps(by_day(rows)))
                .replace("__META__", json.dumps(meta)))
    with open(path, "w") as f:
        f.write(page)
