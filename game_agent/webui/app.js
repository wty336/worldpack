/* Web 前端脚本（Stage A：从 web.py 内嵌字符串拆出，行为不变）。
   自由输入文案由服务端注入到 window.__GAME_FREE_INPUT__（单一真源仍是
   引擎常量 storyline.FREE_INPUT_OPTION），避免前后端各写一份而漂移。 */

let sid = null;
const FREE_INPUT = window.__GAME_FREE_INPUT__;  // 由 index.html 注入（引擎常量）
const $ = (id) => document.getElementById(id);
const story = $("story"), promptEl = $("prompt"), choicesEl = $("choices"), statusEl = $("status"), genEl = $("gen");
let genTimer = null;
let curEntry = null;  // 追加式日志：每回合一个分段，故事连续可回看（玩家反馈：跨天不该清空剧情）

function beginEntry() {
  curEntry = document.createElement("div");
  curEntry.className = "entry";
  story.appendChild(curEntry);
  while (story.children.length > 30) story.removeChild(story.firstChild);  // 上限防 DOM 膨胀
  scrollStory();
}
function scrollStory() {
  story.scrollTop = fullHistory ? story.scrollHeight : 0;  // 回顾看末尾；本轮从头读
}
let fullHistory = false;  // 玩家反馈：故事框默认只放本轮，按钮切换完整历史
function toggleHistory() {
  fullHistory = !fullHistory;
  story.classList.toggle("only-current", !fullHistory);
  const btn = $("historyBtn");
  btn.textContent = fullHistory ? "只看本轮" : "剧情回顾";
  btn.classList.toggle("toggled", fullHistory);
  scrollStory();
}

// 生成态：等待 LLM 期间禁用输入与选项（防重复提交），并显示已等待秒数——
// 玩家实测反馈"不知道是卡了还是模型在思考"。
function setBusy(busy) {
  document.querySelectorAll("#choices button, #inputrow button").forEach((b) => (b.disabled = busy));
  $("in").disabled = busy;
}
function startGen(label) {
  setBusy(true);
  genEl.style.display = "block";
  const t0 = Date.now();
  const tick = () => {
    genEl.textContent = `${label}（已等 ${Math.round((Date.now() - t0) / 1000)} 秒，回答将逐字流式输出）`;
  };
  tick();
  genTimer = setInterval(tick, 1000);
}
function endGen() {
  if (genTimer) { clearInterval(genTimer); genTimer = null; }
  genEl.textContent = "";
  genEl.style.display = "none";
  setBusy(false);
}

async function start() {
  startGen("正在开局");
  try {
    const r = await fetch("/api/new", { method: "POST" });
    const d = await r.json();
    sid = d.sid;
    const name = d.name || "文字养成游戏";
    document.title = name + " · Web";
    $("game-title").textContent = name;
    story.innerHTML = "";
    beginEntry();
    render(d.view);
    await refreshStatus();
  } finally {
    endGen();
  }
}

// 恢复痕迹的中性文案（F1：引擎层不含内容文案，这里只描述机制，不叙述剧情）。
// 玩家实测痛点："不知道是卡了还是模型在思考"——恢复过程此前完全不可见，
// 一次溢出恢复会让本轮多花一次压缩调用（数秒），却没有任何提示。
const RECOVERY_LABELS = {
  overflow_recovered: "本轮上下文超限，已压缩历史后重试成功",
  critique: "本轮初稿未通过自检，已重写",
  meltdown: "本轮多次未达协议，已跳过（效果未生效）",
};
function recoveryNote(v) {
  const parts = (v.recovered || []).map((r) => RECOVERY_LABELS[r] || r);
  if (!parts.length) return "";
  const sub = v.sub_turns > 1 ? "，共生成 " + v.sub_turns + " 次" : "";
  return "（" + parts.join("；") + sub + "）";
}
function render(v) {
  const text = (v.briefing ? v.briefing + "\n" : "") + (v.narration || "");
  const note = recoveryNote(v);
  if (text) {
    curEntry.textContent = text + (note ? "\n\n" + note : "");  // 终稿覆盖流式草稿（同一分段内）
  } else if (note) {
    curEntry.textContent = note;  // 无叙事但有恢复痕迹（如熔断兜底轮）也要说清
  } else if (curEntry && !curEntry.textContent.trim()) {
    curEntry.remove();  // 空分段（如抉择轮直接接管）不留白行
  }
  // 注：恢复痕迹只落在故事分段里，不写 genEl——`endGen()` 在 finally 里清空指示器，
  // 写那里会被立刻冲掉。故事分段是玩家真正会读的地方，也更该留在历史里。
  promptEl.textContent = v.choice_prompt ? ("【关键抉择】" + v.choice_prompt.prompt) : "";
  choicesEl.innerHTML = "";
  const critical = !!v.choice_prompt;  // 关键抉择 → pick 序号；日常选项 → say 文本（与 CLI 同权）
  (v.choices || []).forEach((c, i) => {
    const b = document.createElement("button");
    b.textContent = c;
    b.onclick = critical
      ? () => turn({ kind: "pick", index: i })
      : (c === FREE_INPUT)
        ? () => { $("in").focus(); }
        : () => turn({ kind: "say", text: c });
    choicesEl.appendChild(b);
  });
  if (v.ending) {
    promptEl.textContent = "『" + v.ending.title + "』\n" + (v.ending.text || "");
    choicesEl.innerHTML = "";
  }
  scrollStory();
}
async function turn(req) {
  if (fullHistory) toggleHistory();  // 新回合开始 → 自动回到本轮视图
  startGen("模型思考中");
  beginEntry();
  scrollStory();
  try {
    const resp = await fetch("/api/" + sid + "/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const ev = block.match(/^event: (\w+)/m);
        const data = block.match(/^data: (.*)$/m);
        if (!ev || !data) continue;
        if (ev[1] === "delta") {
          if (genTimer) { clearInterval(genTimer); genTimer = null; }  // 流式开始，指示器让位
          curEntry.textContent += JSON.parse(data[1]);
          scrollStory();
        }
        else if (ev[1] === "done") render(JSON.parse(data[1]));
        else if (ev[1] === "error") {
          if (genTimer) { clearInterval(genTimer); genTimer = null; }
          promptEl.textContent = "[错误] " + JSON.parse(data[1]);
        }
      }
    }
  } finally {
    endGen();  // 无论正常结束/错误/连接中断，都恢复可交互
    try { await refreshStatus(); } catch (e) { /* 状态刷新失败不阻断 */ }
  }
}
function say() {
  const t = $("in").value.trim();
  if (!t) return;
  $("in").value = "";
  turn({ kind: "say", text: t });
}
$("in").addEventListener("keydown", (e) => { if (e.key === "Enter") say(); });
async function refreshStatus() {
  const r = await fetch("/api/" + sid + "/status");
  const d = await r.json();
  statusEl.textContent = d.text || "";
  const ar = await fetch("/api/" + sid + "/actions");
  const ad = await ar.json();
  // 玩家反馈：上方剧情选项与下方日程行动并存，需要讲清分工——
  // 对话推剧情（免费、不耗时）；日程行动是养成（耗行动点 = 推时间，触发后续主线条件）
  if (ad.critical) {
    statusEl.textContent += "\n[关键抉择进行中] 行动暂不可用——先用上方固定选项完成剧情。";
    return;
  }
  statusEl.textContent += "\n[第 " + ad.day + " 天 · 行动点 " + ad.action_points_left
    + "] 今日行动（消耗行动点 = 推进时间；新剧情按天数条件自动触发）——点按钮或直接在对话里说（如「去后山修炼」），结算相同：";
  ad.actions.forEach((a) => {
    const b = document.createElement("button");
    b.textContent = a.label;
    b.onclick = () => turn({ kind: "act", action_id: a.id });
    statusEl.appendChild(b);
  });
  const endBtn = document.createElement("button");
  endBtn.textContent = "结束今天 →";
  endBtn.onclick = () => turn({ kind: "end_day" });
  statusEl.appendChild(endBtn);
  if (ad.action_points_left <= 0) {
    const hint = document.createElement("span");
    hint.className = "t";
    hint.textContent = " 行动点已用完——点「结束今天」进入下一天。";
    statusEl.appendChild(hint);
  }
}
async function doSave() { const r = await fetch("/api/" + sid + "/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: "web.json" }) }); alert((await r.json()).ok ? "已存档" : "失败"); }
async function doLoad() { const r = await fetch("/api/" + sid + "/load", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: "web.json" }) }); const d = await r.json(); if (d.ok) { story.innerHTML = ""; beginEntry(); curEntry.textContent = "（已读档）"; promptEl.textContent = ""; choicesEl.innerHTML = ""; statusEl.textContent = d.status; } else alert("读档失败"); }
start();
