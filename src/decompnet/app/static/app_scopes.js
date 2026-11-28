/* Scopes (app/scopes.py): each part of the game with its own % — the whole code, each binary, what the code does
   (graphics, sound, CD… from the SDK it calls), the focus and your own. Pin the ones you follow; filter the rest. */
const SCOPE_GROUPS = [["pinned", "Pinned"], ["all", "All"], ["code", "Code"], ["binary", "Binaries"], ["area", "Areas"], ["own", "Yours"]];
const SC = { data: null, at: 0, slug: null, group: null, open: new Set() };

function scopePins() {
  try { return new Set(JSON.parse(localStorage.getItem(`scopes:${S.slug}`) || "[]")); } catch { return new Set(); }
}
function saveScopePins(pins) {
  try { localStorage.setItem(`scopes:${S.slug}`, JSON.stringify([...pins])); } catch { /* private window: pins last this visit */ }
}

async function renderScopes(ov, force) {
  const panel = $("scopePanel");
  if (!panel) return;
  panel.hidden = !ov || ov.kind === "analysis" || (ov.kind === "fork" && !ov.ready);
  if (panel.hidden) return;
  if (SC.slug !== S.slug) { SC.slug = S.slug; SC.data = null; SC.group = null; SC.open.clear(); }
  if (force || !SC.data || Date.now() - SC.at > 20000) {
    SC.at = Date.now();
    try { SC.data = await api(`/api/scopes?slug=${encodeURIComponent(S.slug)}`); } catch { return; }
  }
  drawScopes();
}

function drawScopes() {
  const d = SC.data;
  if (!d) return;
  const pins = scopePins();
  if (!SC.group) SC.group = pins.size ? "pinned" : "all";
  const inGroup = (s) => SC.group === "all" || (SC.group === "pinned" ? pins.has(s.key)
    : SC.group === "own" ? ["focus", "project", "mine"].includes(s.group) : s.group === SC.group);
  const count = (g) => d.scopes.filter((s) => g === "all" || (g === "pinned" ? pins.has(s.key)
    : g === "own" ? ["focus", "project", "mine"].includes(s.group) : s.group === g)).length;
  $("scopeGroups").innerHTML = SCOPE_GROUPS.filter(([g]) => g === "all" || count(g))
    .map(([g, label]) => `<button type="button" data-g="${g}" class="${SC.group === g ? "on" : ""}">${label}<span>${count(g)}</span></button>`).join("");
  $("scopeGroups").querySelectorAll("[data-g]").forEach((b) => { b.onclick = () => { SC.group = b.dataset.g; drawScopes(); }; });
  const q = ($("scopeSearch").value || "").trim().toLowerCase();
  const rows = d.scopes.filter((s) => inGroup(s) && (!q || `${s.label} ${s.how}`.toLowerCase().includes(q)));
  $("scopeState").textContent = d.units && d.read < d.units ? `reading the code: ${d.read} / ${d.units} files` : `${d.scopes.length} scopes`;
  $("scopeList").innerHTML = rows.length ? rows.map((s) => {
    const pct = s.percent == null ? null : s.percent;
    const open = SC.open.has(s.key);
    return `<li data-key="${esc(s.key)}">
      <button class="link-btn pin${pins.has(s.key) ? " on" : ""}" data-pin type="button" title="${pins.has(s.key) ? "Unpin" : "Pin: show it under Pinned"}">${pins.has(s.key) ? "★" : "☆"}</button>
      <span class="nm" title="${esc(s.how)}">${esc(s.label)}<small>${esc(s.how)}</small></span>
      <span class="pc">${pct == null ? "–" : pctNum(pct) + "%"}</span>
      <div class="upbar"><i style="width:${pct || 0}%"></i></div>
      <div class="sub"><span>${s.matched.toLocaleString()} / ${s.functions.toLocaleString()} functions</span>
        <span>${s.left ? `${s.left.toLocaleString()} left · ${bytes(s.left_bytes)}` : "all matched"}</span>
        ${s.other ? `<span title="Hand-written assembly, SDK library or code no file covers: not counted">${s.other} not counted</span>` : ""}
        ${s.left ? `<button class="link-btn small" data-left type="button">${open ? "hide what's left" : "what's left"}</button>` : ""}
        ${s.group !== "focus" && s.functions ? `<button class="link-btn small" data-focus type="button" title="Make it the project's focus: with 'Focus first' agents take it before the rest">make it the focus</button>` : ""}
        ${s.removable ? `<button class="link-btn small" data-remove type="button">remove</button>` : ""}</div>
      ${open ? `<div class="left">${s.open.map((f) => `<span class="fn" data-fn="${esc(f.name)}" title="${f.size} bytes">${esc(f.name)}</span>`).join("")}${s.left > s.open.length ? `<span class="muted">… and ${s.left - s.open.length} more</span>` : ""}</div>` : ""}
    </li>`;
  }).join("") : `<li class="muted small">${SC.group === "pinned" ? "Nothing pinned: pin a scope with ☆ to follow it here." : "No scope matches."}</li>`;
  $("scopeList").querySelectorAll("li[data-key]").forEach((li) => {
    const key = li.dataset.key;
    const on = (sel, fn) => { const b = li.querySelector(sel); if (b) b.onclick = fn; };
    on("[data-pin]", () => { const p = scopePins(); p.has(key) ? p.delete(key) : p.add(key); saveScopePins(p); drawScopes(); });
    on("[data-left]", () => { SC.open.has(key) ? SC.open.delete(key) : SC.open.add(key); drawScopes(); });
    on("[data-focus]", () => scopeAction({ action: "focus", key }, "This scope is the project's focus now. Choose “Focus first” in the claim order for agents to take it first."));
    on("[data-remove]", () => scopeAction({ action: "remove", key }, "Scope removed."));
    li.querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
  });
}

async function scopeAction(body, done) {
  try {
    SC.data = await api("/api/scopes", { method: "POST", body: { slug: S.slug, ...body } });
    SC.at = Date.now();
    drawScopes();
    toast(done);
    if (body.action === "focus") { S.ov = await api(`/api/project?slug=${S.slug}`); renderStats(S.ov); }
    return true;
  } catch (e) { toast(esc(e.message), "err"); return false; }
}

$("scopeSearch").oninput = () => drawScopes();
$("scopeAdd").onclick = () => {
  const body = dialog("New scope", `<p class="muted small">A part of the game you want to follow with its own %: the files (units) or functions it covers, as names or globs, separated by spaces. Examples: <code>etc/*</code> (a whole binary), <code>yuugi/main_8002*</code> (files), <code>func_80035894 func_800270D4</code> (functions).</p>
    <label class="field"><span>Name</span><input id="scName" maxlength="60" placeholder="Text engine"></label>
    <label class="field"><span>Covers</span><input id="scMatch" class="mono" placeholder="etc/* func_8002858C"></label>
    <label class="check inline"><input type="checkbox" id="scHelpers"><span>Also the helpers only these functions call</span></label>
    <div class="row"><button class="primary" id="scSave" type="button">Add the scope</button></div>`);
  body.querySelector("#scSave").onclick = async () => {
    const name = body.querySelector("#scName").value.trim();
    if (!await scopeAction({ action: "add", name, match: body.querySelector("#scMatch").value, helpers: body.querySelector("#scHelpers").checked }, `Scope “${esc(name)}” added.`)) return;
    const p = scopePins(); p.add(`mine:${name}`); saveScopePins(p);
    SC.group = "pinned"; drawScopes();
    closeDialog();
  };
};
