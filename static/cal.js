document.addEventListener("DOMContentLoaded", () => {
  calculate();
  loadRandomTips();
});

function calculate() {
  const streaming = +val("streaming");
  const meetings  = +val("meetings");
  const social    = +val("social");
  const gaming    = +val("gaming");
  const messages  = +val("messages");

  const ws = streaming * 55;
  const wm = meetings * 50;
  const wso = social * 30;
  const wg = gaming * 40;
  const wmsg = messages * 0.02 * 7;

  const weekly = ws + wm + wso + wg + wmsg;
  const monthly = (weekly * 4.33) / 1000;

  document.getElementById("result").innerText = monthly.toFixed(1);

  /* SVG GAUGE */
  const maxKg = 20;
  const ratio = Math.min(monthly / maxKg, 1);
  const arcLength = 251;
  const arc = document.getElementById("gaugeArc");

  arc.style.strokeDashoffset = arcLength * (1 - ratio);

  if (monthly <= 8.5) arc.style.stroke = "#22c55e";
  else if (monthly <= 14) arc.style.stroke = "#facc15";
  else arc.style.stroke = "#ef4444";

  const needle = document.getElementById("needle");
  const angle = ratio * 180 - 90;

  needle.style.transform = `rotate(${angle + 8}deg)`;
  setTimeout(() => {
    needle.style.transform = `rotate(${angle}deg)`;
  }, 180);

  /* LIMIT BAR */
  const bar = document.getElementById("progress-bar");
  bar.style.width = Math.min((monthly / 8.5) * 100, 100) + "%";
  bar.style.background = arc.style.stroke;

  document.getElementById("impactMessage").innerText =
    monthly > 8.5
      ? `You are ${(monthly - 8.5).toFixed(1)} kg over the recommended limit.`
      : "You are within the sustainable digital limit.";

  updateLegend(ws, wm, wso, wg, wmsg);

  const trees = Math.max(1, Math.round((monthly * 12) / 21));
  document.getElementById("treeEquivalent").innerText =
    `Your footprint is equivalent to ${trees} mature trees per year.`;
}

/* LEGEND */
function updateLegend(ws, wm, wso, wg, wmsg) {
  const total = ws + wm + wso + wg + wmsg || 1;
  setLegend("streaming", ws, total);
  setLegend("meetings", wm, total);
  setLegend("social", wso, total);
  setLegend("gaming", wg, total);
  setLegend("messaging", wmsg, total);
}

function setLegend(id, value, total) {
  const percent = Math.round((value / total) * 100);
  document.getElementById(`l_${id}`).innerText = percent + "%";
  document.getElementById(`b_${id}`).style.width = percent + "%";
}

/* TIPS */
function loadRandomTips() {
  const tips = [
    "Lower streaming quality from 4K to 1080p",
    "Delete unused cloud files",
    "Disable autoplay videos",
    "Turn off camera when not required",
    "Use Wi-Fi instead of mobile data",
    "Enable dark mode on OLED screens",
    "Limit background app refresh",
    "Unsubscribe from unused newsletters"
  ];

  tips.sort(() => 0.5 - Math.random());

  const list = document.getElementById("tipsList");
  list.innerHTML = "";

  tips.slice(0, 4).forEach(tip => {
    const li = document.createElement("li");
    li.textContent = tip;
    list.appendChild(li);
  });
}

function val(id) {
  return document.getElementById(id).value || 0;
}
