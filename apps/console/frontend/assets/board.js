'use strict';
/* Live scheduling board.
 *
 * Every element here is rendered from the authoritative snapshot:
 *   task_graph.nodes[].scheduling / candidates / eliminated_candidates
 *   scheduling.rounds[]        (SCHEDULING_ROUND events)
 *   scheduler.capabilities[]   (live Agent load published by the Orchestrator)
 *   topology.history[]         (every topology version, for replay)
 *   performance                (measured per-phase wall clock)
 *
 * Motion rule: a card only animates when its real position changed between two
 * observed snapshots. Nothing loops, and nothing moves to suggest activity that
 * the backend did not report.
 */

const board = {
  selectedTask: null,
  topoIndex: null,     // null = follow live
  lastPositions: new Map(),
};

const SCHED_LABELS = {
  BLOCKED: ['等待依赖', 'blocked'],
  CANDIDATE_READY: ['已就绪 · 待调度', 'candidate'],
  QUEUED: ['排队等资源', 'queued'],
  NO_FEASIBLE_AGENT: ['无可行 Agent', 'infeasible'],
  ASSIGNED: ['已分配', 'assigned'],
  RUNNING: ['执行中', 'running'],
  VERIFYING: ['验证中', 'verifying'],
  REASSIGNING: ['故障重分配', 'reassigning'],
  SUCCEEDED: ['已完成', 'succeeded'],
  FAILED: ['失败', 'failed'],
  PAUSED: ['已暂停', 'paused'],
};

function schedLabel(stateName) {
  return (SCHED_LABELS[String(stateName)] || [String(stateName || 'UNKNOWN'), 'neutral'])[0];
}
function schedClass(stateName) {
  return (SCHED_LABELS[String(stateName)] || ['', 'neutral'])[1];
}

function boardNodes() {
  return (state.snapshot?.task_graph?.nodes) || [];
}
function boardAgents() {
  return (state.snapshot?.scheduler?.capabilities || []).filter(x => x.kind === 'agent');
}
function boardPools() {
  return (state.snapshot?.scheduler?.capabilities || []).filter(x => x.kind === 'device');
}
function agentTier(cap) {
  const raw = String(cap?.metadata?.tier || cap?.device_location || '').toLowerCase();
  return ['device', 'edge', 'cloud'].includes(raw) ? raw : 'unknown';
}
function agentRole(cap) {
  const labels = cap?.metadata?.labels || [];
  const role = labels.find(x => !['real-api', 'main-chain', 'role-pool', 'standby', 'agent-studio',
    'local', 'deterministic', 'device-tier', 'auto-fallback', 'fallback'].includes(String(x))
    && !String(x).startsWith('instance-'));
  return String(role || cap?.task_types?.[0] || 'general');
}
function agentLoad(cap) {
  const meta = cap?.metadata || {};
  const running = Number(meta.running_task_count);
  const capacity = Number(meta.max_concurrent_tasks || cap?.capacity || 1);
  if (Number.isFinite(running)) return { running, capacity, measured: true };
  // No live measurement yet: show the configured fraction, labelled as such.
  return { running: Math.round(Number(cap?.current_load || 0) * capacity), capacity, measured: false };
}

/* ---------------------------------------------------------------- solver strip */

function renderBoardSolverStrip() {
  const el = $('boardSolverStrip'), meta = $('boardRoundMeta');
  if (!el) return;
  const sch = state.snapshot?.scheduling;
  const latest = sch?.latest_round;
  if (!latest) {
    el.innerHTML = metricCell('调度轮次', '—', '尚无 SCHEDULING_ROUND 事件');
    if (meta) meta.innerHTML = `<span class="status-dot idle"></span>等待调度决策`;
    return;
  }
  const parallel = Number(latest.assigned_task_count || 0);
  el.innerHTML = [
    metricCell('调度轮次', num(latest.round_index, 0), `共 ${num(sch.round_count, 0)} 轮`),
    metricCell('求解状态', latest.solver_status || '—', latest.policy || ''),
    metricCell('求解耗时', `${num(latest.solve_ms, 1)} ms`, '真实测量'),
    metricCell('本轮并发', num(parallel, 0), `READY ${num(latest.ready_task_count, 0)} · 排队 ${num(latest.queued_task_count, 0)}`),
    metricCell('目标成本', latest.objective_cost === null || latest.objective_cost === undefined ? '—' : num(latest.objective_cost, 4), 'CostModel 分值'),
    metricCell('参与 Agent', num((latest.selected_agents || []).length, 0), '本轮被选中的实例'),
  ].join('');
  if (meta) {
    meta.innerHTML = `<span class="status-dot ${parallel > 0 ? 'run' : 'idle'}"></span>` +
      `第 ${esc(latest.round_index)} 轮 · ${esc(latest.solver_status)} · ${esc(latest.policy)}`;
  }
}

/* ------------------------------------------------------------------- queues */

const QUEUE_ORDER = ['NO_FEASIBLE_AGENT', 'QUEUED', 'CANDIDATE_READY', 'REASSIGNING', 'BLOCKED'];

function renderBoardQueues() {
  const el = $('boardQueues');
  if (!el) return;
  const nodes = boardNodes();
  const groups = new Map(QUEUE_ORDER.map(k => [k, []]));
  for (const node of nodes) {
    const key = String(node.scheduling_state || '');
    if (groups.has(key)) groups.get(key).push(node);
  }
  const blocks = [];
  for (const key of QUEUE_ORDER) {
    const items = groups.get(key) || [];
    if (!items.length) continue;
    blocks.push(`<div class="queue-block ${schedClass(key)}">
      <div class="queue-head"><span class="queue-name">${esc(schedLabel(key))}</span><span class="queue-count">${items.length}</span></div>
      ${items.map(n => taskCard(n, { showReason: true })).join('')}
    </div>`);
  }
  el.innerHTML = blocks.join('') ||
    `<div class="empty-state"><div class="empty-glyph">◎</div><strong>没有等待中的任务</strong><span>所有任务都已进入执行或已完成。</span></div>`;
  bindTaskCards(el);
}

function taskCard(node, opts = {}) {
  const st = String(node.scheduling_state || node.status || '');
  const agent = node.assignment?.agent_id;
  const reason = node.scheduling?.reason || '';
  const selected = board.selectedTask === node.id ? ' selected' : '';
  return `<button type="button" class="board-task ${schedClass(st)}${selected}" data-board-task="${esc(node.id)}" data-flip="${esc(node.id)}">
    <span class="board-task-state">${esc(schedLabel(st))}</span>
    <strong>${esc(short(node.label || node.id, 46))}</strong>
    ${agent ? `<small class="board-task-agent">${esc(agent)}</small>` : ''}
    ${opts.showReason && reason ? `<small class="board-task-reason">${esc(short(reason, 120))}</small>` : ''}
  </button>`;
}

function bindTaskCards(root) {
  root.querySelectorAll('[data-board-task]').forEach(btn => {
    btn.onclick = () => { board.selectedTask = btn.dataset.boardTask; renderBoard(); };
  });
}

/* -------------------------------------------------------------- agent lanes */

function renderBoardLanes() {
  const el = $('boardLanes');
  if (!el) return;
  const agents = boardAgents();
  if (!agents.length) {
    el.innerHTML = `<div class="empty-state"><div class="empty-glyph">◇</div><strong>Agent 池为空</strong><span>启动一次运行后，这里会显示实际注册的 Agent 实例。</span></div>`;
    return;
  }
  const active = new Map();
  for (const node of boardNodes()) {
    const st = String(node.scheduling_state || '');
    const agent = node.assignment?.agent_id;
    if (!agent || !['ASSIGNED', 'RUNNING', 'VERIFYING', 'REASSIGNING'].includes(st)) continue;
    if (!active.has(agent)) active.set(agent, []);
    active.get(agent).push(node);
  }
  const byRole = new Map();
  for (const cap of agents) {
    const role = agentRole(cap);
    if (!byRole.has(role)) byRole.set(role, []);
    byRole.get(role).push(cap);
  }
  const html = [...byRole.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([role, caps]) => {
    const lanes = caps.sort((a, b) => a.actor_id.localeCompare(b.actor_id)).map(cap => {
      const load = agentLoad(cap);
      const pctFull = load.capacity > 0 ? Math.min(100, Math.round(load.running / load.capacity * 100)) : 0;
      const online = cap.online === true;
      const tier = agentTier(cap);
      const running = active.get(cap.actor_id) || [];
      return `<div class="lane ${online ? '' : 'offline'}">
        <div class="lane-head">
          <div class="lane-id"><span class="lane-dot ${online ? 'ok' : 'bad'}"></span>${esc(cap.actor_id)}</div>
          <span class="tier-badge ${esc(tier)}">${esc(tier.toUpperCase())}</span>
        </div>
        <div class="lane-load">
          <div class="lane-bar"><span style="width:${pctFull}%"></span></div>
          <small>${load.running}/${load.capacity} 并发${load.measured ? '' : ' · 未测量'}</small>
        </div>
        <div class="lane-tasks">${running.map(n => taskCard(n)).join('') ||
          `<span class="lane-idle">${online ? '空闲' : '已下线'}</span>`}</div>
      </div>`;
    }).join('');
    return `<div class="lane-group">
      <div class="lane-group-head"><span>${esc(role)}</span><small>${caps.length} 个同类实例</small></div>
      ${lanes}
    </div>`;
  }).join('');
  el.innerHTML = html;
  bindTaskCards(el);
}

/* ------------------------------------------------------------ resource pools */

function renderBoardPools() {
  const el = $('boardPools');
  if (!el) return;
  const pools = boardPools();
  if (!pools.length) { el.innerHTML = `<div class="subtle">尚未注册资源池</div>`; return; }
  const assignments = state.snapshot?.scheduler?.assignments || [];
  const agents = boardAgents();
  const latest = state.snapshot?.scheduling?.latest_round || {};
  const remaining = latest.resource_capacity || {};
  el.innerHTML = pools.sort((a, b) => a.actor_id.localeCompare(b.actor_id)).map(pool => {
    const tier = String(pool.device_location || '').toLowerCase();
    const used = assignments.filter(a => a.resource_id === pool.actor_id).length;
    const residents = agents.filter(a => agentTier(a) === tier).map(a => a.actor_id);
    const free = remaining[pool.actor_id];
    const privacy = pool.metadata?.allowed_privacy_levels || [];
    return `<div class="pool ${esc(tier)}">
      <div class="pool-head"><span class="tier-badge ${esc(tier)}">${esc(tier.toUpperCase())}</span><strong>${esc(pool.actor_id)}</strong></div>
      <div class="pool-rows">
        <div><span>容量</span><strong>${num(pool.capacity, 0)}</strong></div>
        <div><span>本轮可用</span><strong>${free === undefined ? '—' : num(free, 0)}</strong></div>
        <div><span>累计分配</span><strong>${num(used, 0)}</strong></div>
        <div><span>时延</span><strong>${num(pool.latency_ms, 1)} ms</strong></div>
        <div><span>能耗权重</span><strong>${num(pool.energy_cost, 2)}</strong></div>
      </div>
      <div class="pool-privacy"><span>可承载数据等级</span><code>${esc(privacy.join(', ') || '未声明')}</code></div>
      <div class="pool-agents"><span>驻留 Agent</span><code>${esc(residents.join(', ') || '无')}</code></div>
    </div>`;
  }).join('');
}

/* -------------------------------------------------------------- inspector */

function renderBoardInspector() {
  const el = $('boardInspector'), meta = $('boardInspectorMeta');
  if (!el) return;
  const nodes = boardNodes();
  const node = nodes.find(n => n.id === board.selectedTask) || nodes[0];
  if (!node) {
    el.innerHTML = deepEmpty('候选检查器', '选择一个任务后，这里显示它的候选 Agent 与淘汰原因。');
    if (meta) meta.textContent = '';
    return;
  }
  board.selectedTask = node.id;
  const candidates = node.candidates || [];
  const eliminated = node.eliminated_candidates || [];
  const standby = node.standby_candidates || [];
  const summaryRows = Object.entries(node.elimination_summary || {});
  const chosen = node.assignment;
  if (meta) meta.textContent = `${candidates.length} 个候选 · ${eliminated.length} 个被淘汰`;

  const costRow = c => Object.entries(c.cost_breakdown || {})
    .map(([k, v]) => `${k}=${Number(v).toFixed(3)}`).join(' · ');

  el.innerHTML = `
    <div class="inspector-heading">
      <div><h4>${esc(node.label || node.id)}</h4>
        <p class="subtle">${esc(node.id)} · ${esc(schedLabel(node.scheduling_state))}</p></div>
      <span class="state-pill ${statusClass(node.status)}">${esc(node.status)}</span>
    </div>
    <div class="inspector-reason">${esc(node.scheduling?.reason || '无调度说明')}</div>
    <div class="board-two">
      <div>
        <div class="board-sub">参与竞争的候选（按成本升序）</div>
        ${candidates.length ? `<table class="deep-table"><thead><tr><th>Agent</th><th>资源池</th><th>Tier</th><th>总成本</th><th>成本构成</th><th></th></tr></thead><tbody>
          ${candidates.map(c => `<tr class="${chosen && chosen.agent_id === c.agent_id && chosen.resource_id === c.resource_id ? 'chosen' : ''}">
            <td><code>${esc(c.agent_id)}</code></td><td><code>${esc(c.resource_id)}</code></td>
            <td>${esc(String(c.execution_tier || '').toUpperCase())}</td>
            <td>${num(c.total_cost, 4)}</td><td class="tiny">${esc(costRow(c))}</td>
            <td>${chosen && chosen.agent_id === c.agent_id && chosen.resource_id === c.resource_id
              ? '<span class="pick">已选中</span>'
              : (standby.some(s => s.bundle_key === c.bundle_key) ? '<span class="standby">备选</span>' : '')}</td>
          </tr>`).join('')}
        </tbody></table>` : '<div class="subtle">该任务当前没有可行候选。</div>'}
      </div>
      <div>
        <div class="board-sub">被硬约束淘汰的候选</div>
        ${summaryRows.length ? `<div class="elim-summary">${summaryRows.map(([label, count]) =>
          `<span class="elim-chip">${esc(label)}<strong>${num(count, 0)}</strong></span>`).join('')}</div>` : ''}
        ${eliminated.length ? `<div class="elim-list">${eliminated.map(e => `<div class="elim">
          <code>${esc(e.agent_id)} @ ${esc(e.resource_id)}</code>
          <ul>${(e.reasons || []).map(r => `<li>${esc(r)}</li>`).join('')}</ul>
        </div>`).join('')}</div>` : '<div class="subtle">本轮没有候选被淘汰。</div>'}
      </div>
    </div>`;
}

/* ------------------------------------------------------- topology replay */

function topologyVersions() {
  const topo = state.snapshot?.topology || {};
  const history = topo.history || [];
  return history.length ? history : (topo.nodes ? [topo] : []);
}

function renderBoardTopology() {
  const svg = $('boardTopoSvg'), meta = $('boardTopoMeta'), detail = $('boardTopoDetail'), scrub = $('boardTopoScrub');
  if (!svg || !scrub) return;
  const versions = topologyVersions();
  if (!versions.length) {
    svg.innerHTML = '';
    if (meta) meta.textContent = '暂无拓扑版本';
    if (detail) detail.innerHTML = '<div class="subtle">运行开始后，每次拓扑重建都会记录一个可回放版本。</div>';
    scrub.max = 0; scrub.value = 0;
    return;
  }
  const maxIndex = versions.length - 1;
  const index = board.topoIndex === null ? maxIndex : Math.max(0, Math.min(maxIndex, board.topoIndex));
  scrub.max = String(maxIndex);
  scrub.value = String(index);
  const snap = versions[index];
  const prev = index > 0 ? versions[index - 1] : null;
  if (meta) meta.textContent = `版本 ${index + 1}/${versions.length} · v${snap.version ?? '—'}${board.topoIndex === null ? ' · 实时' : ' · 回放'}`;

  const nodes = (snap.nodes || []).map(n => (typeof n === 'string' ? { id: n } : n));
  const edges = (snap.edges || []).map(e => ({
    source: String(e.source ?? e[0] ?? ''), target: String(e.target ?? e[1] ?? ''), score: e.score,
  })).filter(e => e.source && e.target);
  const prevEdges = new Set((prev?.edges || []).map(e => `${e.source ?? e[0]}->${e.target ?? e[1]}`));
  const currentEdges = new Set(edges.map(e => `${e.source}->${e.target}`));
  const prevNodes = new Set((prev?.nodes || []).map(n => String(typeof n === 'string' ? n : n.id)));

  const W = 900, H = 380, cx = W / 2, cy = H / 2, r = Math.min(W, H) / 2 - 70;
  const pos = new Map();
  nodes.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, nodes.length) - Math.PI / 2;
    pos.set(String(n.id), { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) });
  });

  const edgeSvg = edges.map(e => {
    const a = pos.get(e.source), b = pos.get(e.target);
    if (!a || !b) return '';
    const added = prev && !prevEdges.has(`${e.source}->${e.target}`);
    const width = Number.isFinite(Number(e.score)) ? 1 + 3 * Math.max(0, Number(e.score)) : 1;
    return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" class="topo-edge ${added ? 'added' : ''}" stroke-width="${width}"></line>`;
  }).join('');
  const removedSvg = (prev?.edges || []).map(e => {
    const s = String(e.source ?? e[0] ?? ''), t = String(e.target ?? e[1] ?? '');
    if (!s || !t || currentEdges.has(`${s}->${t}`)) return '';
    const a = pos.get(s), b = pos.get(t);
    if (!a || !b) return '';
    return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" class="topo-edge removed" stroke-width="1"></line>`;
  }).join('');
  const nodeSvg = nodes.map(n => {
    const p = pos.get(String(n.id));
    const isNew = prev && !prevNodes.has(String(n.id));
    return `<g class="topo-node ${isNew ? 'added' : ''}" transform="translate(${p.x},${p.y})">
      <circle r="16"></circle>
      <text y="32" text-anchor="middle">${esc(short(String(n.id), 18))}</text>
    </g>`;
  }).join('');
  svg.innerHTML = `<g class="topo-layer">${removedSvg}${edgeSvg}${nodeSvg}</g>`;

  const rebuilds = state.snapshot?.topology?.rebuild_events || [];
  const match = rebuilds.find(r => r.version === snap.version);
  if (detail) {
    detail.innerHTML = [
      truthRow('节点数', nodes.length),
      truthRow('边数', edges.length),
      truthRow('连通性 λ2', snap.lambda2_or_connectivity ?? '—'),
      truthRow('连通', snap.connected === true ? '是' : snap.connected === false ? '否' : '—'),
      match ? truthRow('新增边', (match.added_edges || []).join(', ') || '无') : '',
      match ? truthRow('删除边', (match.removed_edges || []).join(', ') || '无') : '',
      match ? truthRow('受影响 Agent', (match.affected_agents || []).join(', ') || '无') : '',
      match ? truthRow('重建原因', match.reason || '—') : '',
    ].join('');
  }
}

/* ---------------------------------------------------------- perf attribution */

function renderBoardPerf() {
  const el = $('boardPerf'), meta = $('boardPerfMeta');
  if (!el) return;
  const perf = state.snapshot?.performance;
  if (!perf || !perf.sample_count) {
    el.innerHTML = '<div class="subtle">尚无阶段耗时样本。任务执行后会记录 TASK_PHASE_TIMING 事件。</div>';
    if (meta) meta.textContent = '';
    return;
  }
  if (meta) meta.textContent = `${perf.sample_count} 个任务样本`;
  const totals = perf.totals_ms || {}, share = perf.share_of_task_time || {};
  const phases = [['agent_plan_ms', '模型调用'], ['tool_execution_ms', '工具执行'], ['verification_ms', '独立验证']];
  el.innerHTML = `
    <div class="perf-bars">${phases.map(([key, name]) => {
      const pctValue = Number(share[key] || 0);
      return `<div class="perf-row">
        <span class="perf-name">${esc(name)}</span>
        <div class="perf-bar"><span style="width:${Math.min(100, Math.round(pctValue * 100))}%"></span></div>
        <span class="perf-value">${num(totals[key], 1)} ms · ${(pctValue * 100).toFixed(1)}%</span>
      </div>`;
    }).join('')}</div>
    <div class="board-sub">最慢的任务</div>
    ${deepTable(['任务', 'Agent', '总耗时 ms', '模型 ms', '工具 ms', '验证 ms'],
      (perf.slowest_tasks || []).map(row => [
        short(row.task_id, 30), short(row.agent_id || '—', 30),
        num(row.timings_ms?.total_ms, 1), num(row.timings_ms?.agent_plan_ms, 1),
        num(row.timings_ms?.tool_execution_ms, 1), num(row.timings_ms?.verification_ms, 1),
      ]))}
    <p class="subtle">${esc(perf.measurement || '')}</p>`;
}


/* ---------------------------------------------------------- dynamic DAG */

const dagView = { scale: 1, tx: 0, ty: 0, round: null, selected: null, lastLevels: new Map() };

function dagRounds() { return state.snapshot?.scheduling?.rounds || []; }

/* Task states as of the end of a given scheduling round, replayed from the
 * authoritative SCHEDULING_ROUND diagnostics. Replay never invents a state:
 * a task with no diagnostic yet at that point is simply not yet decided. */
function dagStateAtRound(index) {
  const rounds = dagRounds();
  if (index === null || index >= rounds.length) return null;
  const seen = new Map();
  for (let i = 0; i <= index && i < rounds.length; i += 1) {
    for (const task of rounds[i].tasks || []) {
      if (task.task_id) seen.set(String(task.task_id), { state: task.state, round: rounds[i].round_index, reason: task.reason, selected: task.selected });
    }
  }
  return seen;
}

function renderDag() {
  const svg = $('dagSvg'), meta = $('dagMeta'), legend = $('dagLegend'), scrub = $('dagScrub');
  if (!svg || !scrub) return;
  const graph = state.snapshot?.task_graph;
  const nodes = graph?.nodes || [];
  if (!nodes.length) {
    svg.innerHTML = '';
    if (meta) meta.textContent = '等待任务图';
    if (legend) legend.innerHTML = '';
    scrub.max = 0;
    return;
  }
  const edges = graph.edges || [];
  const rounds = dagRounds();
  const live = dagView.round === null;
  const replay = live ? null : dagStateAtRound(dagView.round);
  scrub.max = String(Math.max(0, rounds.length - 1));
  scrub.value = String(live ? Math.max(0, rounds.length - 1) : dagView.round);

  const criticalOn = $('dagCritical')?.checked !== false;
  const critical = new Set(criticalOn ? (graph.critical_path || []) : []);

  // Layered layout: one column per dependency level, parallel work spread down.
  const byLevel = new Map();
  for (const node of nodes) {
    const lvl = Number(node.level || 0);
    if (!byLevel.has(lvl)) byLevel.set(lvl, []);
    byLevel.get(lvl).push(node);
  }
  const levels = [...byLevel.keys()].sort((a, b) => a - b);
  const colW = 260, rowH = 96, padX = 40, padY = 40;
  const maxRows = Math.max(1, ...levels.map(l => byLevel.get(l).length));
  const width = padX * 2 + Math.max(1, levels.length) * colW;
  const height = padY * 2 + maxRows * rowH;
  const pos = new Map();
  levels.forEach((lvl, col) => {
    const column = byLevel.get(lvl).slice().sort((a, b) => String(a.id).localeCompare(String(b.id)));
    const offset = (maxRows - column.length) / 2;
    column.forEach((node, row) => {
      pos.set(String(node.id), { x: padX + col * colW, y: padY + (row + offset) * rowH, node });
    });
  });

  const nodeState = node => {
    if (replay) {
      const seen = replay.get(String(node.id));
      return seen ? seen.state : 'BLOCKED';
    }
    return schedState(node);
  };

  const edgeSvg = edges.map(e => {
    const a = pos.get(String(e.source)), b = pos.get(String(e.target));
    if (!a || !b) return '';
    const x1 = a.x + 196, y1 = a.y + 30, x2 = b.x, y2 = b.y + 30;
    const mid = (x1 + x2) / 2;
    const hot = critical.has(String(e.source)) && critical.has(String(e.target));
    return `<path d="M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}" class="dag-link ${hot ? 'critical' : ''}"></path>`;
  }).join('');

  const nodeSvg = nodes.map(node => {
    const p = pos.get(String(node.id));
    if (!p) return '';
    const st = nodeState(node);
    const cls = schedClass(st);
    const hot = critical.has(String(node.id));
    const agent = node.assignment?.agent_id;
    const changed = state.changedTasks.has(node.id) ? ' just-changed' : '';
    const chosen = dagView.selected === node.id ? ' selected' : '';
    const dur = Number.isFinite(Number(node.duration_ms)) ? `${num(node.duration_ms, 0)} ms` : '';
    return `<g class="dag-card ${cls}${hot ? ' critical' : ''}${changed}${chosen}" transform="translate(${p.x},${p.y})" data-dag-node="${esc(node.id)}">
      <rect width="196" height="60" rx="10"></rect>
      <text class="dag-state" x="12" y="19">${esc(schedLabel(st))}${hot ? ' · 关键路径' : ''}</text>
      <text class="dag-title" x="12" y="37">${esc(short(node.label || node.id, 24))}</text>
      <text class="dag-agent" x="12" y="52">${esc(short(agent || '—', 20))}</text>
      ${dur ? `<text class="dag-dur" x="184" y="52" text-anchor="end">${esc(dur)}</text>` : ''}
    </g>`;
  }).join('');

  const labels = levels.map((lvl, col) =>
    `<text class="dag-level" x="${padX + col * colW}" y="20">L${lvl}</text>`).join('');

  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.innerHTML = `<g class="dag-pan" transform="translate(${dagView.tx},${dagView.ty}) scale(${dagView.scale})">${labels}${edgeSvg}${nodeSvg}</g>`;
  svg.querySelectorAll('[data-dag-node]').forEach(g => {
    g.addEventListener('click', () => {
      dagView.selected = g.dataset.dagNode;
      board.selectedTask = g.dataset.dagNode;
      renderBoard();
    });
  });

  if (meta) {
    meta.textContent = live
      ? `${nodes.length} 节点 · ${graph.layer_count} 层 · 关键路径 ${(graph.critical_path || []).length} 步 · 实时`
      : `回放第 ${rounds[dagView.round]?.round_index ?? dagView.round + 1} 轮 / 共 ${rounds.length} 轮`;
  }
  if (legend) {
    const counts = graph.scheduling_state_counts || {};
    legend.innerHTML = Object.entries(counts).map(([k, v]) =>
      `<span class="dag-chip ${schedClass(k)}">${esc(schedLabel(k))}<strong>${num(v, 0)}</strong></span>`).join('')
      + `<span class="dag-note">${esc(graph.critical_path_source || '')}</span>`;
  }
}

function wireDag() {
  const svg = $('dagSvg');
  if (!svg) return;
  let dragging = false, startX = 0, startY = 0;
  svg.addEventListener('pointerdown', e => {
    if (e.target.closest('[data-dag-node]')) return;
    dragging = true; startX = e.clientX - dagView.tx; startY = e.clientY - dagView.ty;
    svg.setPointerCapture(e.pointerId); svg.classList.add('grabbing');
  });
  svg.addEventListener('pointermove', e => {
    if (!dragging) return;
    dagView.tx = e.clientX - startX; dagView.ty = e.clientY - startY;
    const g = svg.querySelector('.dag-pan');
    if (g) g.setAttribute('transform', `translate(${dagView.tx},${dagView.ty}) scale(${dagView.scale})`);
  });
  const stop = e => { dragging = false; svg.classList.remove('grabbing'); try { svg.releasePointerCapture(e.pointerId); } catch (_) {} };
  svg.addEventListener('pointerup', stop);
  svg.addEventListener('pointercancel', stop);
  svg.addEventListener('wheel', e => {
    e.preventDefault();
    dagView.scale = Math.min(2.5, Math.max(0.35, dagView.scale * (e.deltaY < 0 ? 1.1 : 0.9)));
    renderDag();
  }, { passive: false });

  const zoom = factor => { dagView.scale = Math.min(2.5, Math.max(0.35, dagView.scale * factor)); renderDag(); };
  $('dagZoomIn').onclick = () => zoom(1.2);
  $('dagZoomOut').onclick = () => zoom(1 / 1.2);
  $('dagFit').onclick = () => { dagView.scale = 1; dagView.tx = 0; dagView.ty = 0; renderDag(); };
  $('dagCritical').onchange = renderDag;
  $('dagLive').onclick = () => { dagView.round = null; renderDag(); };
  $('dagScrub').oninput = () => { dagView.round = Number($('dagScrub').value); renderDag(); };
  const step = delta => {
    const rounds = dagRounds();
    if (!rounds.length) return;
    const current = dagView.round === null ? rounds.length - 1 : dagView.round;
    dagView.round = Math.max(0, Math.min(rounds.length - 1, current + delta));
    renderDag();
  };
  $('dagStepBack').onclick = () => step(-1);
  $('dagStepFwd').onclick = () => step(1);
}

/* ------------------------------------------------- memory pipeline view */

function memPacks() { return state.snapshot?.memory?.context_packs || {}; }

function renderMemoryPipeline() {
  const el = $('memPipeline'), meta = $('memPipeMeta');
  if (!el) return;
  const mem = state.snapshot?.memory;
  const pipe = mem?.pipeline;
  if (!pipe || !pipe.sample_count) {
    el.innerHTML = '<div class="subtle">尚无 ContextPack。任务开始执行后会记录完整的记忆选择过程。</div>';
    if (meta) meta.textContent = '';
    $('memPackSelect').innerHTML = '';
    $('memPackSummary').innerHTML = '';
    $('memRanked').innerHTML = '';
    $('memDropped').innerHTML = '';
    return;
  }
  if (meta) meta.textContent = `${pipe.sample_count} 个 ContextPack · ${num(pipe.stale_record_count, 0)} 条记忆已失效`;
  const t = pipe.totals || {};
  const funnel = [
    ['全量历史', t.full_history_records, '本 Run 全部可召回记忆'],
    ['候选召回', t.raw_candidates, '五路召回累计（含重复）'],
    ['去重后', t.deduplicated_candidates, '同一条记忆只保留一次'],
    ['权限/状态淘汰', t.filtered_out, 'REJECTED/STALE 或越权'],
    ['强制核心', t.forced_core, '核心约束不参与竞争，必定入选'],
    ['进入 ContextPack', t.selected, '最终唤醒给 Agent 的条目'],
  ];
  const peak = Math.max(1, ...funnel.map(x => Number(x[1] || 0)));
  el.innerHTML = funnel.map(([name, value, note], i) => `
    <div class="mem-stage">
      <div class="mem-stage-head"><span>${esc(name)}</span><strong>${num(value, 0)}</strong></div>
      <div class="mem-stage-bar"><span style="width:${Math.round(Number(value || 0) / peak * 100)}%"></span></div>
      <small>${esc(note)}</small>
      ${i < funnel.length - 1 ? '<span class="mem-arrow">↓</span>' : ''}
    </div>`).join('');

  const packs = memPacks();
  const ids = Object.keys(packs);
  const select = $('memPackSelect');
  const current = ids.includes(state.memPack) ? state.memPack : ids[0];
  state.memPack = current;
  select.innerHTML = ids.map(id => `<option value="${esc(id)}"${id === current ? ' selected' : ''}>${esc(short(id, 34))}</option>`).join('');
  select.onchange = () => { state.memPack = select.value; renderMemoryPipeline(); };
  renderMemoryPack(packs[current]);
}

function renderMemoryPack(pack) {
  const summary = $('memPackSummary'), ranked = $('memRanked'), dropped = $('memDropped');
  if (!pack) { summary.innerHTML = ''; ranked.innerHTML = ''; dropped.innerHTML = ''; return; }
  const trace = pack.selection_trace || {};
  const comp = trace.compression || {};
  summary.innerHTML = [
    truthRow('全量历史 Token（估算）', num(pack.full_history_token_estimate, 0)),
    truthRow('ContextPack Token（估算）', num(pack.token_estimate, 0)),
    truthRow('压缩比', pack.full_history_token_estimate ? pct(1 - pack.token_estimate / pack.full_history_token_estimate) : '—'),
    truthRow('是否截断', pack.truncated ? '是' : '否'),
    truthRow('唤醒条目数', num((pack.memory_ids || []).length, 0)),
    truthRow('硬约束', num((pack.hard_constraints || []).length, 0)),
    truthRow('禁止项', num((pack.prohibitions || []).length, 0)),
  ].join('') + `<div class="mem-comp">
    <span>去重移除</span><code>${esc(JSON.stringify(comp.deduplicated || {}))}</code>
    <span>条数上限移除</span><code>${esc(JSON.stringify(comp.item_limit_dropped || {}))}</code>
    <span>Token 预算移除</span><code>${esc(JSON.stringify(comp.budget_dropped || {}))}</code>
  </div>`;

  const rows = trace.ranked || [];
  ranked.innerHTML = rows.length ? `<table class="deep-table"><thead><tr><th>记忆</th><th>类型</th><th>分数</th><th>状态</th><th></th></tr></thead><tbody>
    ${rows.slice(0, 24).map(r => `<tr class="${r.selected ? 'chosen' : ''}">
      <td>${esc(short(r.summary || r.memory_id, 40))}</td>
      <td>${esc(r.memory_type)}</td>
      <td>${r.is_core_constraint ? '<span class="pick">强制</span>' : num(r.score, 3)}</td>
      <td>${esc(r.verification_status)}</td>
      <td>${r.selected ? '<span class="pick">入选</span>' : '<span class="standby">未入选</span>'}</td>
    </tr>`).join('')}</tbody></table>` : '<div class="subtle">没有候选记忆。</div>';

  const cut = trace.dropped_by_recall_limit || [];
  const filtered = trace.filtered_out || [];
  const stale = state.snapshot?.memory?.pipeline?.stale_records || [];
  dropped.innerHTML = `
    <div class="board-sub">淘汰原因</div>
    <div class="mem-drop-cols">
      <div><strong>召回上限之外 (${cut.length})</strong>${cut.length ? `<ul>${cut.slice(0, 8).map(r =>
        `<li>${esc(short(r.summary || r.memory_id, 46))} · ${num(r.score, 3)}</li>`).join('')}</ul>` : '<p class="subtle">无</p>'}</div>
      <div><strong>权限/状态过滤 (${filtered.length})</strong>${filtered.length ? `<ul>${filtered.slice(0, 8).map(r =>
        `<li>${esc(short(r.summary || r.memory_id, 40))} — ${esc(short(r.drop_reason, 52))}</li>`).join('')}</ul>` : '<p class="subtle">无</p>'}</div>
      <div><strong>证据失效导致 STALE (${stale.length})</strong>${stale.length ? `<ul>${stale.slice(0, 8).map(r =>
        `<li>${esc(short(r.summary || r.memory_id, 46))}</li>`).join('')}</ul>` : '<p class="subtle">无</p>'}</div>
    </div>
    <p class="subtle">${esc(trace.ranking_formula || '')}</p>`;
}

/* ------------------------------------------------------------------- FLIP */

function captureBoardPositions() {
  const map = new Map();
  document.querySelectorAll('#view-board [data-flip]').forEach(el => {
    map.set(el.dataset.flip, el.getBoundingClientRect());
  });
  return map;
}

function playBoardTransitions(before) {
  // A card animates only if the backend actually moved it (different lane/queue).
  document.querySelectorAll('#view-board [data-flip]').forEach(el => {
    const prev = before.get(el.dataset.flip);
    if (!prev) return;
    const next = el.getBoundingClientRect();
    const dx = prev.left - next.left, dy = prev.top - next.top;
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
    el.animate(
      [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'translate(0, 0)' }],
      { duration: 420, easing: 'cubic-bezier(.22,.61,.36,1)' },
    );
  });
}

/* ------------------------------------------------------------------ render */

function renderBoard() {
  if (typeof state === 'undefined') return;
  const before = captureBoardPositions();
  renderBoardSolverStrip();
  renderBoardQueues();
  renderBoardLanes();
  renderBoardPools();
  renderBoardInspector();
  renderDag();
  renderMemoryPipeline();
  renderBoardTopology();
  renderBoardPerf();
  if (state.view === 'board') playBoardTransitions(before);
}

/* -------------------------------------------------------------- wiring */

(function wireBoard() {
  titles.board = ['LIVE SCHEDULING', '调度看板'];
  const prevRender = renderCurrentView;
  window.renderCurrentView = function () {
    if (state.view === 'board') { renderChrome(); renderBoard(); return; }
    prevRender();
  };
  // app.js calls renderCurrentView by name, so replace the binding it resolves.
  renderCurrentView = window.renderCurrentView;

  wireDag();
  const scrub = $('boardTopoScrub');
  if (scrub) scrub.oninput = () => { board.topoIndex = Number(scrub.value); renderBoardTopology(); };
  const prev = $('boardTopoPrev'), next = $('boardTopoNext'), live = $('boardTopoLive');
  const shift = delta => {
    const versions = topologyVersions();
    if (!versions.length) return;
    const current = board.topoIndex === null ? versions.length - 1 : board.topoIndex;
    board.topoIndex = Math.max(0, Math.min(versions.length - 1, current + delta));
    renderBoardTopology();
  };
  if (prev) prev.onclick = () => shift(-1);
  if (next) next.onclick = () => shift(1);
  if (live) live.onclick = () => { board.topoIndex = null; renderBoardTopology(); };
})();
