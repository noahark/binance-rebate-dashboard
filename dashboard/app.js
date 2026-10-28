// Real Dashboard Application Controller connecting to Backend REST APIs
const $ = s => document.querySelector(s);

function maskNumber(val) {
    if (val === null || val === undefined || val === '' || val === '-') return '-';
    const s = String(val).trim();
    if (s === '-' || s === '待同步') return s;
    const isNeg = s.startsWith('-');
    const clean = isNeg ? s.slice(1) : s;
    const dotIdx = clean.indexOf('.');
    const intStr = dotIdx !== -1 ? clean.slice(0, dotIdx) : clean;
    const decStr = dotIdx !== -1 ? clean.slice(dotIdx) : '';
    const digits = intStr.replace(/\D/g, '');
    if (!digits) return s;
    let maskedInt = '';
    if (digits.length <= 1) {
        maskedInt = digits;
    } else if (digits.length === 2) {
        maskedInt = digits[0] + '*' + digits[1];
    } else {
        const starCount = Math.min(digits.length - 2, 4);
        maskedInt = digits[0] + '*'.repeat(starCount) + digits[digits.length - 1];
    }
    return (isNeg ? '-' : '') + maskedInt + decStr;
}

function maskName(name) {
    if (!name || name === '-') return '-';
    const s = String(name).trim();
    if (!s) return '-';
    const arr = Array.from(s);
    if (arr.length <= 1) return s;
    if (arr.length === 2) {
        return arr[0] + '*' + arr[1];
    }
    const starCount = Math.min(arr.length - 2, 4);
    return arr[0] + '*'.repeat(starCount) + arr[arr.length - 1];
}

function maskUid(uid) {
    if (!uid) return '';
    const s = String(uid).trim();
    if (s.length <= 2) return s;
    if (s.length <= 5) return s[0] + '**' + s.slice(-1);
    return s.slice(0, 3) + '****' + s.slice(-2);
}

function formatCustomerName(name) {
    if (!name || name === '-') return '-';
    return state.hideSensitive ? maskName(name) : name;
}

function formatUid(uid) {
    if (!uid) return '';
    return state.hideSensitive ? maskUid(uid) : uid;
}

function formatText(text) {
    if (!text || !state.hideSensitive) return text || '';
    return String(text).replace(/(?<![-/\d])\d+(?:,\d{3})*(?:\.\d+)?(?![-\d/])/g, match => {
        if (!match.includes('.') && !match.includes(',') && match.length < 3) return match;
        return maskNumber(match);
    });
}

const money = (n, digits = 2) => {
    if (n === null || n === undefined) return '-';
    const num = Number(String(n).replace(/,/g, ''));
    if (isNaN(num)) return '-';
    const formatted = num.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return state.hideSensitive ? maskNumber(formatted) : formatted;
};
const escapeHtml = s => String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let state = {
    page: 'overview',
    days: 15,
    market: 'all',
    tab: 'all',
    search: '',
    sort: 'commission',
    excludeTop: 0,
    referenceEnd: '2026-10-15',
    hideSensitive: localStorage.getItem('hideSensitive') === '1',
};

let cache = {
    overview: null,
    customers: null,
    currentDetail: null,
    insights: null,
    dynamics: null,
    imports: null,
    care: null,
};

let careState = {
    tab: 'workbench',
    filter: '全部',
    code: '',
    search: '',
    sort: 'history',
    page: 0,
    pageSize: 8,
};

let openedUid = null;
let focusReturn = null;
let timer = null;

function toast(message) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(() => el.hidden = true, 3500);
}

async function api(path, options = {}) {
    try {
        const res = await fetch(path, {
            headers: { 'Content-Type': 'application/json' },
            ...options,
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || err.message || `HTTP ${res.status}`);
        }
        return await res.json();
    } catch (e) {
        console.error(`API Error [${path}]:`, e);
        toast(`接口调用异常: ${e.message}`);
        return null;
    }
}

const marketLabel = () => '全业务汇总';

function change(d) {
    if (d === null || d === undefined) return '<span class="muted">无对比数据</span>';
    return `<span class="${d >= 0 ? 'up' : 'down'}">${d >= 0 ? '↗ +' : '↘ '}${Number(d).toFixed(1)}%</span>`;
}

function metric(label, value, unit, note, primary = false) {
    return `<article class="metric ${primary ? 'primary' : ''}"><div class="metric-label">${label}<span>↗</span></div><div class="metric-value">${value}<small>${unit}</small></div><div class="metric-note">${note}</div></article>`;
}

function spark(points) {
    if (!points || !points.length) return '<span class="muted">-</span>';
    const group = Array.from({ length: 15 }, (_, i) => points[Math.min(points.length - 1, Math.floor(i * points.length / 15))]);
    const max = Math.max(...group, 1);
    return `<svg class="spark" viewBox="0 0 68 23" aria-label="期间返佣趋势"><path d="${group.map((v, i) => `${i ? 'L' : 'M'}${i * 4.7},${21 - v / max * 18}`).join(' ')}" fill="none" stroke="${group.at(-1) >= group[0] ? '#6eab94' : '#d09486'}" stroke-width="1.5"/></svg>`;
}

function formatTick(v) {
    if (state.hideSensitive) {
        if (v === 0) return '0';
        return '***';
    }
    if (v >= 1000000000) return (v / 1000000000).toFixed(1) + 'B';
    if (v >= 1000000) return (v / 1000000).toFixed(1) + 'M';
    if (v >= 1000) return (v / 1000).toFixed(1) + 'k';
    return Number(v).toFixed(0);
}

function chart(points, previous = [], chartId = 'detail', dates = [], fullDates = []) {
    if (!points || !points.length || points.every(v => v === 0)) {
        return '<div class="empty" style="padding:40px 10px">暂无该期间的日级时序数据，需执行历史回补以生成趋势折线。</div>';
    }
    const count = points.length;
    const vals = points;
    const prev = (previous && previous.length === count) ? previous : points.map(() => 0);
    const hasPrev = prev.some(v => v > 0);
    const maxVal = Math.max(...vals, ...(hasPrev ? prev : [0]), 1);
    const max = maxVal * 1.15;

    const step = count > 1 ? 640 / (count - 1) : 640;
    const path = a => a.map((v, i) => `${i ? 'L' : 'M'}${48 + i * step},${180 - (v / max) * 150}`).join(' ');

    const ticks = Array.from({ length: 4 }, (_, i) => {
        const v = max * i / 3, y = 180 - (i / 3) * 150;
        const label = formatTick(v);
        return `<line x1="48" y1="${y}" x2="688" y2="${y}" stroke="#edf0f3" stroke-dasharray="3 4"/><text x="38" y="${y + 4}" text-anchor="end" font-size="10" fill="#94a3b8">${label}</text>`;
    }).join('');

    const dateStep = count <= 8 ? 1 : (count <= 16 ? 2 : Math.ceil(count / 7));
    const dateTicks = dates && dates.length ? dates.map((d, i) => {
        if (i % dateStep !== 0 && i !== count - 1) return '';
        const x = 48 + i * step;
        return `<line x1="${x}" y1="180" x2="${x}" y2="185" stroke="#cbd5e1"/><text x="${x}" y="200" text-anchor="middle" font-size="10" fill="#8f99a7">${d}</text>`;
    }).join('') : '';

    return `<svg viewBox="0 0 710 225" preserveAspectRatio="xMidYMid meet" role="img" aria-label="趋势图" data-chart-id="${chartId}">
        <defs>
            <linearGradient id="fill-${chartId}" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stop-color="#e6c470" stop-opacity=".22"/>
                <stop offset="100%" stop-color="#e6c470" stop-opacity="0"/>
            </linearGradient>
        </defs>
        ${ticks}
        ${dateTicks}
        <path d="${path(vals)} L688,180 L48,180Z" fill="url(#fill-${chartId})"/>
        ${hasPrev ? `<path d="${path(prev)}" fill="none" stroke="#b0b8c2" stroke-width="1.5" stroke-dasharray="4 4"/>` : ''}
        <path d="${path(vals)}" fill="none" stroke="#d2a744" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
        <line class="linked-cursor" x1="48" x2="48" y1="20" y2="180" visibility="hidden"/>
        <circle class="linked-marker prev" cx="48" cy="180" r="3.5" visibility="hidden"/>
        <circle class="linked-marker current" cx="48" cy="180" r="5" visibility="hidden"/>
        <rect class="chart-overlay" x="48" y="15" width="640" height="175" fill="transparent"/>
    </svg>`;
}

function dualAxisChart(commPoints = [], volPoints = [], prevCommPoints = [], prevVolPoints = [], dates = [], fullDates = [], syncedMask = []) {
    const hasData = (commPoints && commPoints.some(v => v > 0)) || (volPoints && volPoints.some(v => v > 0));
    if (!hasData) {
        return '<div class="empty" style="padding:40px 10px">暂无该期间的日级时序数据，需执行历史回补以生成趋势折线。</div>';
    }

    const count = Math.max(commPoints.length, volPoints.length, dates.length);
    if (count <= 1) {
        return '<div class="empty" style="padding:40px 10px">数据点不足以生成趋势折线。</div>';
    }

    const commVals = commPoints && commPoints.length === count ? commPoints : Array(count).fill(0);
    const volVals = volPoints && volPoints.length === count ? volPoints : Array(count).fill(0);
    const prevComm = prevCommPoints && prevCommPoints.length === count ? prevCommPoints : Array(count).fill(0);
    const prevVol = prevVolPoints && prevVolPoints.length === count ? prevVolPoints : Array(count).fill(0);

    const hasPrevComm = prevComm.some(v => v > 0);
    const hasPrevVol = prevVol.some(v => v > 0);

    const maxCommVal = Math.max(...commVals, ...(hasPrevComm ? prevComm : [0]), 1);
    const maxComm = maxCommVal * 1.15;

    const maxVolVal = Math.max(...volVals, ...(hasPrevVol ? prevVol : [0]), 1);
    const maxVol = maxVolVal * 1.15;

    // ViewBox: 0 0 760 260
    // Chart plotting area: x from 55 to 705 (width = 650), y from 25 to 210 (height = 185)
    const step = 650 / (count - 1);
    const getX = i => 55 + i * step;
    const getYComm = v => 210 - (Math.max(0, v) / maxComm) * 185;
    const getYVol = v => 210 - (Math.max(0, v) / maxVol) * 185;

    const pathComm = (arr) => arr.map((v, i) => `${i ? 'L' : 'M'}${getX(i).toFixed(1)},${getYComm(v).toFixed(1)}`).join(' ');
    const pathVol = (arr) => arr.map((v, i) => `${i ? 'L' : 'M'}${getX(i).toFixed(1)},${getYVol(v).toFixed(1)}`).join(' ');

    // 5 horizontal grid levels
    const levels = [0, 0.25, 0.5, 0.75, 1.0];
    const ticks = levels.map(pct => {
        const y = (210 - pct * 185).toFixed(1);
        const commV = maxComm * pct;
        const volV = maxVol * pct;
        return `
            <line x1="55" y1="${y}" x2="705" y2="${y}" stroke="#edf0f3" stroke-dasharray="3 4"/>
            <text x="47" y="${(Number(y) + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="#99772b" font-weight="500">${formatTick(commV)}</text>
            <text x="713" y="${(Number(y) + 3.5).toFixed(1)}" text-anchor="start" font-size="10" fill="#2563eb" font-weight="500">${formatTick(volV)}</text>
        `;
    }).join('');

    // X-axis date labels
    const dateStep = count <= 8 ? 1 : (count <= 16 ? 2 : (count <= 31 ? 4 : Math.ceil(count / 8)));
    const dateTicks = dates && dates.length ? dates.map((d, i) => {
        if (i % dateStep !== 0 && i !== count - 1) return '';
        const x = getX(i).toFixed(1);
        return `
            <line x1="${x}" y1="210" x2="${x}" y2="215" stroke="#cbd5e1"/>
            <text x="${x}" y="232" text-anchor="middle" font-size="10" fill="#8f99a7">${d}</text>
        `;
    }).join('') : '';

    return `<svg viewBox="0 0 760 260" preserveAspectRatio="xMidYMid meet" role="img" aria-label="客户返佣与交易额双轴趋势图" data-chart-id="dual-trends">
        <defs>
            <linearGradient id="fill-dual-comm" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stop-color="#d2a744" stop-opacity="0.12"/>
                <stop offset="100%" stop-color="#d2a744" stop-opacity="0.0"/>
            </linearGradient>
            <linearGradient id="fill-dual-vol" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stop-color="#2563eb" stop-opacity="0.06"/>
                <stop offset="100%" stop-color="#2563eb" stop-opacity="0.0"/>
            </linearGradient>
        </defs>

        <!-- Background grid and tick labels -->
        ${ticks}

        <!-- Axis top unit labels -->
        <text x="47" y="16" fill="#99772b" font-size="9.5" font-weight="600" text-anchor="end" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif">USD</text>
        <text x="713" y="16" fill="#2563eb" font-size="9.5" font-weight="600" text-anchor="start" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif">USD/USDT</text>

        <!-- Axis border lines -->
        <line x1="55" y1="210" x2="705" y2="210" stroke="#cbd5e1" stroke-width="1"/>
        <line x1="55" y1="25" x2="55" y2="210" stroke="#e8dab5" stroke-width="1.2"/>
        <line x1="705" y1="25" x2="705" y2="210" stroke="#c0d7fa" stroke-width="1.2"/>

        <!-- X Axis date ticks -->
        ${dateTicks}

        <!-- Subtle area fills under current period curves -->
        <path d="${pathVol(volVals)} L705,210 L55,210Z" fill="url(#fill-dual-vol)"/>
        <path d="${pathComm(commVals)} L705,210 L55,210Z" fill="url(#fill-dual-comm)"/>

        <!-- Curves: Trade Volume (Blue) - Previous period dashed, current solid -->
        ${hasPrevVol ? `<path class="curve-prev-vol" d="${pathVol(prevVol)}" fill="none" stroke="#2563eb" stroke-width="1.6" stroke-dasharray="4 4" opacity="0.65" stroke-linecap="round"/>` : ''}
        <path class="curve-current-vol" d="${pathVol(volVals)}" fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>

        <!-- Curves: Rebate Commission (Gold) - Previous period dashed, current solid -->
        ${hasPrevComm ? `<path class="curve-prev-comm" d="${pathComm(prevComm)}" fill="none" stroke="#d2a744" stroke-width="1.6" stroke-dasharray="4 4" opacity="0.75" stroke-linecap="round"/>` : ''}
        <path class="curve-current-comm" d="${pathComm(commVals)}" fill="none" stroke="#d2a744" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>

        <!-- Interactive Crosshair -->
        <line class="linked-cursor" x1="55" x2="55" y1="20" y2="210" stroke="#94a3b8" stroke-dasharray="3 3" stroke-width="1.2" visibility="hidden"/>

        <!-- 4 Markers -->
        <circle class="linked-marker prev-vol" cx="55" cy="210" r="3.5" visibility="hidden"/>
        <circle class="linked-marker current-vol" cx="55" cy="210" r="4.5" visibility="hidden"/>
        <circle class="linked-marker prev-comm" cx="55" cy="210" r="3.5" visibility="hidden"/>
        <circle class="linked-marker current-comm" cx="55" cy="210" r="4.5" visibility="hidden"/>

        <!-- Mouse capture overlay -->
        <rect class="chart-overlay" x="55" y="15" width="650" height="205" fill="transparent"/>
    </svg>`;
}

function linkedTrends(ov) {
    const t = ov.trends || {};
    const m = ov.metrics || {};
    const ex = ov.exclusion || {};
    const excludedCount = ex.excluded_count || 0;
    const excludedNames = ex.excluded_names || [];
    const dates = t.dates || [];
    const fullDates = t.full_dates || [];
    const hasPrev = (t.previous_commission && t.previous_commission.some(v => v > 0)) ||
                    (t.previous_volume && t.previous_volume.some(v => v > 0));

    const covText = t.is_complete
        ? `每日汇总 · 已完整覆盖全部 ${t.total_days} 天日级时序`
        : (t.has_daily_data
            ? `日级覆盖 ${t.synced_days_count || 0}/${t.total_days || state.days} 天 · 剩余历史天数可一键回补`
            : `日级数据待回补`);

    return `<section class="panel linked-trends" id="linkedTrendsPanel">
        <div class="panel-head" style="display:flex;justify-content:space-between;align-items:flex-start">
            <div>
                <h2>客户返佣与交易额走势 (双 Y 轴)</h2>
                <p class="panel-sub">${marketLabel()} · 左轴返佣(金色)与右轴交易额(蓝色)合并联动 · ${covText}</p>
            </div>
            ${!t.is_complete ? `
                <button class="button secondary" id="syncTrendPeriodBtn" data-days="${state.days}" style="font-size:11px;padding:4px 10px;margin-top:2px">
                    ⚡ 补全本期 (${state.days}天) 日级趋势
                </button>
            ` : ''}
        </div>
        <div class="exclusion-toolbar">
            <span>排除大客户</span>
            <div role="group" aria-label="排除返佣排名靠前的客户">
                ${[0, 1, 3, 5, 10, 20].map(n => `<button data-exclude-top="${n}" aria-pressed="${state.excludeTop === n}" class="${state.excludeTop === n ? 'selected' : ''}">${n ? '前 ' + n + ' 大' : '不排除'}</button>`).join('')}
            </div>
        </div>
        <div class="scope-note">按本期返佣排名 · 已排除 ${excludedCount} 人${excludedCount > 0 ? `（排除返佣体量 ${state.hideSensitive ? maskNumber(ex.excluded_commission || '0.00') : (ex.excluded_commission || '0.00')}，交易额 ${state.hideSensitive ? maskNumber(ex.excluded_volume || '0.00') : (ex.excluded_volume || '0.00')}）` : ''}，剩余 ${ex.included_count || 0} 人。联动总览与指标。</div>
        ${excludedNames.length ? `<div class="excluded-list"><span>已排除大客户</span><p>${excludedNames.map(n => escapeHtml(formatCustomerName(n))).join('、')}</p></div>` : ''}
        <div class="chart-legend dual-legend">
            <div class="legend-group">
                <span class="legend-axis-title left">◀ 左 Y 轴 · 返佣</span>
                <span class="legend-chip"><i class="legend-line solid gold"></i>本期 (实线)</span>
                <span class="legend-chip"><i class="legend-line dashed gold"></i>上期同日 ${hasPrev ? '(虚线)' : '(未回补)'}</span>
            </div>
            <div class="legend-divider"></div>
            <div class="legend-group">
                <span class="legend-axis-title right">右 Y 轴 · 交易额 ▶</span>
                <span class="legend-chip"><i class="legend-line solid blue"></i>本期 (实线)</span>
                <span class="legend-chip"><i class="legend-line dashed blue"></i>上期同日 ${hasPrev ? '(虚线)' : '(未回补)'}</span>
            </div>
        </div>
        ${t.has_daily_data ? `
            <div class="linked-readout" id="linkedReadout" aria-live="polite">${t.notice}</div>
            <div class="linked-chart-block dual-chart-block">
                <div class="dual-chart-heading">
                    <div class="dual-heading-metric comm">
                        <span class="axis-dot gold"></span>
                        <span class="axis-title">返佣 (左轴)</span>
                        <span class="axis-readout" id="value-commission">
                            <span class="readout-date">本期合计</span>: <strong class="readout-val gold">${money(m.total_commission || 0)}</strong>
                        </span>
                    </div>
                    <div class="dual-heading-metric vol">
                        <span class="axis-dot blue"></span>
                        <span class="axis-title">交易额 (右轴)</span>
                        <span class="axis-readout" id="value-volume">
                            <span class="readout-date">本期合计</span>: <strong class="readout-val blue">${money(m.total_volume || 0)}</strong>
                        </span>
                    </div>
                </div>
                <div class="chart linked-chart" data-linked-chart="dual">
                    ${dualAxisChart(t.current_commission, t.current_volume, t.previous_commission, t.previous_volume, dates, fullDates, t.synced_mask)}
                </div>
            </div>
        ` : `
            <div class="empty">${t.notice}<br><br><button class="button secondary" id="triggerSyncFromTrend" style="margin-top:10px">前往数据导入 / 同步页进行回补 →</button></div>
        `}
        <div class="chart-tip">左 Y 轴为返佣额度（金色），右 Y 轴为名义交易额（蓝色）；实线为本期走势，虚线为上期同日对照。在图表任意位置滑动均可同步查看双轴当日数据与较上期波动。</div>
    </section>`;
}


function overview() {
    const ov = cache.overview;
    if (!ov) return '<div class="empty">正在加载总览数据…</div>';

    const m = ov.metrics || {};
    const cov = ov.coverage || {};
    const top = ov.top_customers || [];

    return `
        <div class="metrics">
            ${metric('本期返佣', money(m.total_commission || 0), 'USDT', `<b>${change(m.delta_pct)}</b> 对比前 ${state.days} 天`, true)}
            ${metric('本期交易额', m.total_volume ? money(Number(m.total_volume) / 10000, 1) : '-', '万', '来自 API 真实名义交易额')}
            ${metric('产生返佣客户', cov.active_customers || 0, '人', cov.note || '')}
            ${metric('重点关注客户', ov.star_summary?.starred_count || 0, '人', `库内客户总计 ${cov.total_customers || 0} 人`)}
        </div>
        <div class="content-grid">
            ${linkedTrends(ov)}
        </div>
        <div class="lower-grid">
            <section class="panel">
                <div class="panel-head"><h2>值得关注</h2><span class="pill warn">待跟进</span></div>
                <div class="attention-list">
                    <button class="attention-item" data-filter="decline">
                        <span class="attention-icon">↘</span>
                        <div><strong>贡献下降客户</strong><p>较前一周期返佣显著下降<br>安排一次跟进了解客户近况</p></div>
                        <span class="arrow">›</span>
                    </button>
                    <button class="attention-item" data-filter="silent">
                        <span class="attention-icon">◷</span>
                        <div><strong>本期无返佣客户</strong><p>已同步但在本期未产生佣金<br>核实客户交易节奏</p></div>
                        <span class="arrow">›</span>
                    </button>
                    <button class="attention-item" data-filter="star">
                        <span class="attention-icon green">☆</span>
                        <div><strong>重点关注名单</strong><p>查看已加星客户的最新表现<br>跟进沟通与服务需求</p></div>
                        <span class="arrow">›</span>
                    </button>
                </div>
            </section>
            <section class="panel ranking">
                <div class="panel-head">
                    <div><h2>客户贡献排名 <span style="color:#b1b7c0;font-size:10px;font-weight:400;margin-left:8px">TOP 10</span></h2><p class="panel-sub">按本期返佣排序 · 全业务汇总</p></div>
                    <button class="text-button" data-page="customers">查看全部客户 ↗</button>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>客户</th>
                                <th>本期返佣 (USDT)</th>
                                <th>交易额 (USDT)</th>
                                <th>上期返佣 (USDT)</th>
                                <th>较上期</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${top.map((c, i) => `<tr data-client-uid="${c.user_id}"><td><span class="rank ${i < 3 ? 'top' : ''}">${String(i + 1).padStart(2, '0')}</span><span class="customer-name">${escapeHtml(formatCustomerName(c.name))}</span><button class="star-toggle-btn ${c.is_starred ? 'starred' : ''}" data-star-uid="${c.user_id}" title="${c.is_starred ? '已重点关注，点击取消' : '点击加入重点关注'}">${c.is_starred ? '★' : '☆'}</button><div class="uid">UID ${formatUid(c.user_id)} · VIP ${c.vip_level}</div></td><td><strong>${money(c.commission)}</strong></td><td>${money(c.volume)}</td><td>${c.prev_commission !== null && c.prev_commission !== undefined ? money(c.prev_commission) : '<span class="muted">-</span>'}</td><td>${change(c.delta_pct)}</td><td><button class="mini-button" data-client-uid="${c.user_id}">查看详情 ↗</button></td></tr>`).join('')}
                        </tbody>
                    </table>
                    ${top.length ? '' : '<div class="empty">暂无客户贡献数据，请先同步本期数据。</div>'}
                </div>
                <div class="table-foot"><span>★ 已加入重点关注</span><span>点击任意一行查看详情 →</span></div>
            </section>
        </div>
    `;
}

let missingSync = { busy: false, message: '' };

function missingDataNotice() {
    const c = cache.customers?.coverage;
    if (!c) return '';
    const missing = c.current_missing + c.previous_missing;
    if (!missing && !missingSync.message && !missingSync.busy) return '';
    return `<div class="notice" role="status" style="display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap">
        <div>${missing ? `数据待补齐：本期 ${c.current_missing} 户，上期 ${c.previous_missing} 户（全客户库）。` : '本期与上期数据已完整。'}
        <div style="margin-top:6px">${escapeHtml(missingSync.message || '仅补周期交易量与佣金；已有有效数据跳过，日趋势需单独回补。')}</div></div>
        ${missing || missingSync.busy ? `<button class="button secondary" id="syncMissingCustomers" ${missingSync.busy ? 'disabled' : ''}>${missingSync.busy ? '同步中…' : '同步缺失数据'}</button>` : ''}
    </div>`;
}

async function syncMissingCustomers() {
    if (missingSync.busy) return;
    const days = state.days, referenceEnd = state.referenceEnd;
    missingSync = { busy: true, message: `正在检查截至 ${referenceEnd} 的本期及上期各 ${days} 天数据…` };
    await render();
    const started = await api('/api/dashboard/sync/start', {
        method: 'POST',
        body: JSON.stringify({ scope: 'all', days, reference_end: referenceEnd, granularity: 'period' }),
    });
    if (!started) {
        missingSync = { busy: false, message: '同步未能启动，请重试。' };
        await render();
        return;
    }
    let missingPolls = 0;
    async function poll() {
        const res = await api(`/api/dashboard/sync/status?job_id=${encodeURIComponent(started.job_id)}`);
        const j = res?.latest_job;
        if (!j) {
            if (++missingPolls < 5) { setTimeout(poll, 2000); return; }
            missingSync = { busy: false, message: '暂时无法读取任务进度，请到数据导入与同步查看任务状态。' };
        } else if (j.status === 'running') {
            missingPolls = 0;
            missingSync.message = `${referenceEnd} · ${days} 天周期：已处理 ${j.processed_items}/${j.total_items} 项，失败 ${j.failed_items} 项。`;
            if (['customers', 'followups'].includes(state.page)) await render();
            setTimeout(poll, 2000);
            return;
        } else {
            missingSync = { busy: false, message: j.status === 'completed'
                ? (j.failed_items > 0 ? `同步结束，${j.failed_items} 项仍未补齐。可重试；超出 API 历史范围的数据需历史快照补充。` : '同步完成，已刷新本期与上期统计。')
                : '任务未完成，可重试或在数据导入与同步中查看详情。' };
            await reloadCurrentPageData();
        }
        if (['customers', 'followups'].includes(state.page)) await render();
    }
    poll();
}

function customers() {
    const res = cache.customers;
    if (!res) return '<div class="empty">正在加载客户列表…</div>';
    const items = res.customers || [];

    return `${missingDataNotice()}<section class="panel">
        <div class="customer-toolbar">
            <div class="tabs">
                ${[['all', '全部客户'], ['star', '重点关注'], ['decline', '贡献下降'], ['silent', '本期无返佣']].map(([k, v]) => `<button data-tab="${k}" class="${state.tab === k ? 'selected' : ''}">${v}</button>`).join('')}
            </div>
            <input class="search" id="search" placeholder="搜索客户备注、UID 或推荐码" aria-label="搜索客户" value="${escapeHtml(state.search)}">
        </div>
        <div class="panel-head" style="padding-top:0;padding-bottom:15px">
            <span class="sort">${items.length} 位客户 · 真实本地库 · 全业务汇总</span>
            <select id="sort" aria-label="客户排序">
                <option value="commission" ${state.sort === 'commission' ? 'selected' : ''}>按本期返佣金额排序</option>
                <option value="decline" ${state.sort === 'decline' ? 'selected' : ''}>按返佣减少额排序</option>
            </select>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr><th>客户</th><th>返佣 (USDT)</th><th>交易额 (USDT)</th><th>较上期</th><th>返佣趋势</th><th>最近返佣</th><th>状态</th></tr>
                </thead>
                <tbody>
                    ${items.map((c, i) => `
                        <tr data-client-uid="${c.user_id}" tabindex="0" aria-label="查看 ${escapeHtml(formatCustomerName(c.name))}">
                            <td>
                                <span class="rank ${i < 3 ? 'top' : ''}">${String(i + 1).padStart(2, '0')}</span>
                                <span class="customer-name">${escapeHtml(formatCustomerName(c.name))}</span>
                                <button class="star-toggle-btn ${c.is_starred ? 'starred' : ''}" data-star-uid="${c.user_id}" title="${c.is_starred ? '已重点关注，点击取消' : '点击加入重点关注'}">${c.is_starred ? '★' : '☆'}</button>
                                <div class="uid">UID ${formatUid(c.user_id)} · ${escapeHtml(c.referral_code || '-')} · VIP ${c.vip_level}</div>
                            </td>
                            <td><strong>${money(c.commission)}</strong></td>
                            <td>${money(c.volume)}</td>
                            <td>${change(c.delta_pct)}</td>
                            <td>${spark(c.sparkline)}</td>
                            <td>${c.last_rebate_date}</td>
                            <td><span class="pill ${c.has_synced ? '' : 'warn'}">${c.has_synced ? '已同步' : '未同步'}</span></td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
            ${items.length ? '' : '<div class="empty">没有符合条件的客户，试试其他筛选条件。</div>'}
        </div>
        <div class="table-foot">
            <span>客户名单来自本地客户库（由 CSV 导入初始化）</span>
            <span>共 ${items.length} 位</span>
        </div>
    </section>`;
}

function getCareFilteredCustomers() {
    const careData = cache.care;
    if (!careData) return { list: [], scoped: [], pages: 1 };
    const all = careData.customers || [];
    const scoped = all.filter(u => {
        const matchCode = !careState.code || u.code === careState.code;
        const matchSearch = !careState.search || (u.name + ' ' + u.uid + ' ' + (u.code || '')).toLowerCase().includes(careState.search.toLowerCase());
        return matchCode && matchSearch;
    });
    const list = scoped.filter(u => {
        if (careState.filter === '全部') return true;
        return u.status === careState.filter;
    }).sort((a, b) => {
        if (careState.sort === 'decline') return a.delta - b.delta;
        if (careState.sort === 'current') return b.current - a.current;
        return b.total - a.total;
    });
    const pages = Math.max(1, Math.ceil(list.length / careState.pageSize));
    careState.page = Math.min(careState.page, pages - 1);
    return { list, scoped, pages };
}

function renderCareRows() {
    const { list, scoped, pages } = getCareFilteredCustomers();
    const rowsEl = $('#careRows');
    const chipsEl = $('#careChips');
    const resultCountEl = $('#careResultCount');
    const listSubtitleEl = $('#careListSubtitle');
    const pageLabelEl = $('#carePageLabel');
    const prevBtn = $('#carePrevPage');
    const nextBtn = $('#careNextPage');

    if (resultCountEl) resultCountEl.textContent = `${list.length} 户`;
    if (listSubtitleEl) {
        listSubtitleEl.textContent = (careState.code ? careState.code + ' · ' : '') + '本期与上个等长周期对比 · 金额来源于官方底层 API';
    }

    if (chipsEl) {
        const statuses = ['全部', '沉睡', '降温', '活跃', '新星', '数据待补齐'];
        chipsEl.innerHTML = statuses.map(s => {
            const count = scoped.filter(u => s === '全部' || u.status === s).length;
            return `<button class="${careState.filter === s ? 'selected' : ''}" data-care-filter="${s}">${s} <span>${count}</span></button>`;
        }).join('');
    }

    if (rowsEl) {
        const paged = list.slice(careState.page * careState.pageSize, (careState.page + 1) * careState.pageSize);
        if (!paged.length) {
            rowsEl.innerHTML = '<tr><td colspan="5" class="empty">没有符合条件的客户，可切换状态筛选或清空搜索。</td></tr>';
        } else {
            rowsEl.innerHTML = paged.map(u => {
                const tagClass = u.status === '活跃' ? 'active' : u.status === '降温' ? 'cooling' : u.status === '沉睡' ? 'sleeping' : u.status === '新星' ? 'newstar' : 'missing';
                return `
                    <tr>
                        <td>
                            <button class="name-button" data-care-uid="${u.uid}">${escapeHtml(formatCustomerName(u.name))}</button>
                            <span class="tag ${tagClass}">${u.status}</span>
                            <div class="person-sub">${formatUid(u.uid)} · VIP ${u.vip}</div>
                        </td>
                        <td class="number">
                            ${money(u.total)}
                            <div class="amount-sub">全时段快照</div>
                        </td>
                        <td class="number">
                            ${u.missing ? '—' : money(u.current)}
                            <div class="amount-sub">${u.missing ? '等待同步' : '上期 ' + money(u.previous)}</div>
                        </td>
                        <td>
                            ${u.missing ? '<span class="muted">数据待补齐</span>' : `${change(u.delta)}<br>${spark(u.sparkline)}`}
                        </td>
                        <td>
                            <button class="row-button ${u.has_followed ? 'followed' : ''}" data-care-uid="${u.uid}">
                                ${u.has_followed ? '已跟进 ✓' : (u.missing ? '查看缺口' : '查看与跟进')}
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        }
    }

    if (pageLabelEl) pageLabelEl.textContent = `共 ${list.length} 户 · 第 ${careState.page + 1} / ${pages} 页`;
    if (prevBtn) prevBtn.disabled = careState.page === 0;
    if (nextBtn) nextBtn.disabled = careState.page >= pages - 1;
}

function followups() {
    const careData = cache.care;
    if (!careData) return '<div class="empty">正在加载唤醒与关怀数据…</div>';

    const summary = careData.summary || {};
    const total = summary.total_customers || 0;
    const missing = summary.missing_count || 0;
    const review = careData.followup_review || { contacted_count: 0, pending_count: 0, waiting_count: 0, logs: [] };
    const { list, scoped, pages } = getCareFilteredCustomers();

    // 1. Top Notice Bar
    const noticeHtml = `
        <div class="sync-alert ${missing === 0 ? 'done' : ''}" role="status">
            <div>
                <strong>${missingSync.busy ? '正在后台向 SAPI 请求回补数据…' : missing > 0 ? `数据覆盖 ${total - missing} / ${total} 户 · ${missing} 户数据待补齐` : `数据已补齐 · ${total} / ${total} 户可参与识别`}</strong><br>
                ${missingSync.busy ? escapeHtml(missingSync.message) : missing > 0 ? '缺本期或上期对比数据的客户不判沉睡。点击右侧按钮回补，已有数据秒级跳过。' : '本期与上个等长周期对比数据完备，已重新识别客户活跃状态。'}
            </div>
            ${missing > 0 ? `<button class="button secondary" id="careSyncBtn" ${missingSync.busy ? 'disabled' : ''}>${missingSync.busy ? '同步中…' : '↻ 回补缺失数据'}</button>` : '<span style="font-size:11px;color:#3b7156">本地数据完备 ✓</span>'}
        </div>
    `;

    // 2. Tabs
    const tabsHtml = `
        <div class="care-tabs">
            <button class="${careState.tab === 'workbench' ? 'selected' : ''}" data-care-tab="workbench">唤醒工作台</button>
            <button class="${careState.tab === 'channels' ? 'selected' : ''}" data-care-tab="channels">推荐码观察</button>
            <button class="${careState.tab === 'review' ? 'selected' : ''}" data-care-tab="review">跟进复盘 <span>${review.logs ? review.logs.length : 0}</span></button>
            <div style="margin-left:auto"><button class="text-button" id="openRulesBtn" style="font-size:12px;color:#8a94a2">识别规则 ↗</button></div>
        </div>
    `;

    // 3. Workbench Tab
    const statusCards = [
        { label: '沉睡客户', key: '沉睡', count: summary.sleeping_count || 0, icon: '↗', note: '近期贡献明显减少，适合了解近况' },
        { label: '降温客户', key: '降温', count: summary.cooling_count || 0, icon: '◷', note: '关注变化原因，及时提供支持' },
        { label: '活跃关怀', key: '活跃', count: summary.active_count || 0, icon: '♡', note: '关注服务体验，定期了解交易近况' },
        { label: '新星培育', key: '新星', count: summary.newstar_count || 0, icon: '✧', note: '首次交易后的使用体验与需求' }
    ];

    const workbenchHtml = `
        <div id="careWorkbenchView" ${careState.tab !== 'workbench' ? 'hidden' : ''}>
            <div class="care-metrics">
                ${statusCards.map(c => `
                    <button class="metric ${careState.filter === c.key ? 'selected' : ''}" data-care-filter="${c.key}">
                        <div class="metric-label">${c.label}<span>${c.icon}</span></div>
                        <div class="metric-value">${c.count}<small>户</small></div>
                        <div class="metric-note">${c.note}</div>
                    </button>
                `).join('')}
            </div>
            <div class="work-grid">
                <section class="panel">
                    <div class="panel-head">
                        <div>
                            <h2>待联系名单 <span class="count" id="careResultCount">${list.length} 户</span></h2>
                            <p class="sub" id="careListSubtitle">${(careState.code ? careState.code + ' · ' : '')}本期与上个等长周期对比 · 金额来源于官方底层 API</p>
                        </div>
                        <button class="text-button" id="exportCareBtn">导出名单 ↓</button>
                    </div>
                    <div class="toolbar">
                        <div class="chips" id="careChips">
                            ${['全部', '沉睡', '降温', '活跃', '新星', '数据待补齐'].map(s => {
                                const count = scoped.filter(u => s === '全部' || u.status === s).length;
                                return `<button class="${careState.filter === s ? 'selected' : ''}" data-care-filter="${s}">${s} <span>${count}</span></button>`;
                            }).join('')}
                        </div>
                        <div class="search-row">
                            <input type="search" id="careSearch" value="${escapeHtml(careState.search)}" placeholder="搜索备注、UID" aria-label="搜索备注或 UID">
                            <select id="careCodeFilter" aria-label="推荐码筛选">
                                <option value="">全部推荐码</option>
                                ${(careData.referral_codes || []).map(r => `
                                    <option value="${escapeHtml(r.code)}" ${careState.code === r.code ? 'selected' : ''}>${escapeHtml(r.code)} · ${escapeHtml(r.note || '未命名')}</option>
                                `).join('')}
                            </select>
                            <select id="careSort" aria-label="排序">
                                <option value="history" ${careState.sort === 'history' ? 'selected' : ''}>历史贡献优先</option>
                                <option value="decline" ${careState.sort === 'decline' ? 'selected' : ''}>下降幅度优先</option>
                                <option value="current" ${careState.sort === 'current' ? 'selected' : ''}>本期贡献优先</option>
                            </select>
                        </div>
                    </div>
                    <div class="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>客户 / 状态</th>
                                    <th>历史佣金</th>
                                    <th>本期佣金</th>
                                    <th>较上期 / 节奏</th>
                                    <th>下一步</th>
                                </tr>
                            </thead>
                            <tbody id="careRows">
                                ${(!list.length) ? '<tr><td colspan="5" class="empty">没有符合条件的客户，可切换状态筛选或清空搜索。</td></tr>' : list.slice(careState.page * careState.pageSize, (careState.page + 1) * careState.pageSize).map(u => {
                                    const tagClass = u.status === '活跃' ? 'active' : u.status === '降温' ? 'cooling' : u.status === '沉睡' ? 'sleeping' : u.status === '新星' ? 'newstar' : 'missing';
                                    return `
                                        <tr>
                                            <td>
                                                <button class="name-button" data-care-uid="${u.uid}">${escapeHtml(formatCustomerName(u.name))}</button>
                                                <span class="tag ${tagClass}">${u.status}</span>
                                                <div class="person-sub">${formatUid(u.uid)} · VIP ${u.vip}</div>
                                            </td>
                                            <td class="number">
                                                ${money(u.total)}
                                                <div class="amount-sub">全时段快照</div>
                                            </td>
                                            <td class="number">
                                                ${u.missing ? '—' : money(u.current)}
                                                <div class="amount-sub">${u.missing ? '等待同步' : '上期 ' + money(u.previous)}</div>
                                            </td>
                                            <td>
                                                ${u.missing ? '<span class="muted">数据待补齐</span>' : `${change(u.delta)}<br>${spark(u.sparkline)}`}
                                            </td>
                                            <td>
                                                <button class="row-button ${u.has_followed ? 'followed' : ''}" data-care-uid="${u.uid}">
                                                    ${u.has_followed ? '已跟进 ✓' : (u.missing ? '查看缺口' : '查看与跟进')}
                                                </button>
                                            </td>
                                        </tr>
                                    `;
                                }).join('')}
                            </tbody>
                        </table>
                    </div>
                    <div class="table-foot">
                        <span id="carePageLabel">共 ${list.length} 户 · 第 ${careState.page + 1} / ${pages} 页</span>
                        <div>
                            <button class="text-button" id="carePrevPage" ${careState.page === 0 ? 'disabled' : ''}>← 上一页</button>　
                            <button class="text-button" id="careNextPage" ${careState.page >= pages - 1 ? 'disabled' : ''}>下一页 →</button>
                        </div>
                    </div>
                </section>
                <aside class="panel care-sidebar">
                    <div class="panel-head">
                        <div>
                            <h2>关怀，也要主动</h2>
                            <p class="sub">不只在客户沉默时出现</p>
                        </div>
                    </div>
                    <div id="careSignalsList">
                        ${(careData.care_signals || []).map(s => `
                            <div class="care-item">
                                <div class="event-label">●　${escapeHtml(s.label)}</div>
                                <h3>${escapeHtml(formatCustomerName(s.name))}</h3>
                                <p>${escapeHtml(formatText(s.desc))}</p>
                                <button class="text-button" data-care-uid="${s.uid}">查看关怀建议 →</button>
                            </div>
                        `).join('')}
                    </div>
                    <div class="side-note">
                        <b>提醒有依据，联系有温度。</b><br>
                        基于客户全生命周期返佣与近期活跃状态。所有提醒均为您日常维护提供依据，不自动发送消息。
                    </div>
                </aside>
            </div>
        </div>
    `;

    // 4. Channels Tab
    const channelsHtml = `
        <div id="careChannelsView" ${careState.tab !== 'channels' ? 'hidden' : ''}>
            <section class="panel">
                <div class="panel-head">
                    <div>
                        <h2>看见每个推荐码背后的人</h2>
                        <p class="sub">渠道历史绩效与当前样本分层分别呈现 · 点击查看名单联动筛选</p>
                    </div>
                    <span class="demo-pill">全时段渠道快照</span>
                </div>
                <div class="table-wrap" style="margin-top:20px">
                    <table>
                        <thead>
                            <tr>
                                <th>推荐码 / 备注</th>
                                <th>渠道邀请人数</th>
                                <th>样本 / 沉睡＋降温</th>
                                <th>历史交易量</th>
                                <th>历史佣金</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${(careData.referral_codes || []).map(c => `
                                <tr>
                                    <td>
                                        <strong>${escapeHtml(c.code)}</strong>
                                        <div class="person-sub">${escapeHtml(c.note || '未命名')}</div>
                                    </td>
                                    <td>${c.count}</td>
                                    <td>${c.sample_count} / ${c.sample_risk_count}</td>
                                    <td>${money(c.volume)}</td>
                                    <td>${money(c.total)}</td>
                                    <td><button class="row-button" data-filter-code="${escapeHtml(c.code)}">查看名单 →</button></td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
                <p class="footnote" style="padding:14px 22px;color:#9aa3af;font-size:11px;margin:0">
                    渠道邀请人数覆盖整个推荐码，样本包含已导入客户。人群结构不同，建议结合客群特征进行针对性维护。
                </p>
            </section>
        </div>
    `;

    // 5. Review Tab
    const reviewLogs = review.logs || [];
    const reviewHtml = `
        <div id="careReviewView" ${careState.tab !== 'review' ? 'hidden' : ''}>
            <section class="panel">
                <div class="panel-head">
                    <div>
                        <h2>每次联系，都留下下一步</h2>
                        <p class="sub">本地数据库持久化保存的真实跟进与沟通历史</p>
                    </div>
                    <span class="demo-pill">真实跟进记录</span>
                </div>
                <div class="progress-cards">
                    <div>已记录跟进<b>${review.contacted_count || 0} <small>户</small></b></div>
                    <div>待跟进流失风险<b>${review.pending_count || 0} <small>户</small></b></div>
                    <div>待下周期回暖确认<b>${review.waiting_count || 0} <small>户</small></b></div>
                </div>
                <div id="careLogsList">
                    ${reviewLogs.length ? reviewLogs.map(l => `
                        <div class="log-row">
                            <strong>${escapeHtml(formatCustomerName(l.customer_name))}</strong> (UID ${formatUid(l.user_id)})　<span class="tag active">已跟进</span>
                            <div style="margin:6px 0;color:#334155">${escapeHtml(formatText(l.note))}</div>
                            <small>下次跟进：${l.followup_date || '未安排'} · 记录时间：${(l.created_at || '').slice(0, 16).replace('T', ' ')}</small>
                        </div>
                    `).join('') : '<div class="empty">暂无历史跟进记录。<br>可在工作台待联系名单中打开客户记录跟进。</div>'}
                </div>
                <p class="footnote" style="padding:14px 22px;color:#9aa3af;font-size:11px;margin:0">
                    回暖效果需要下一完整周期数据确认，不将单次交易增长直接归因于一次联系。
                </p>
            </section>
        </div>
    `;

    return `
        ${noticeHtml}
        ${tabsHtml}
        ${workbenchHtml}
        ${channelsHtml}
        ${reviewHtml}
    `;
}

function openCareModal(uid) {
    const careData = cache.care;
    if (!careData) return;
    const u = (careData.customers || []).find(c => c.uid === uid);
    if (!u) return;

    const modal = $('#careModal');
    if (!modal) return;

    const tagClass = u.status === '活跃' ? 'active' : u.status === '降温' ? 'cooling' : u.status === '沉睡' ? 'sleeping' : u.status === '新星' ? 'newstar' : 'missing';
    const reasonText = u.missing
        ? '本期或上期缺数据，暂不判断活跃状态。请点击顶部的“回补缺失数据”按钮。'
        : (u.status === '沉睡' || u.status === '降温')
        ? '联系依据：历史有持续贡献，本期较上期明显减少。建议先核实客户近期策略周期、账户调整或服务诉求，不直接推断离开平台。'
        : (u.status === '新星')
        ? '培育依据：近期新注册或首笔交易客户，适合主动了解接入流程、费率以及交易工具使用体验。'
        : '关怀建议：持续活跃的核心贡献客户，定期了解交易体验与高阶工具/专属服务支持需求。';

    modal.innerHTML = `
        <button class="close" id="closeCareModal" aria-label="关闭">×</button>
        <div class="eyebrow">CUSTOMER CARE</div>
        <h2>${escapeHtml(formatCustomerName(u.name))} <span class="tag ${tagClass}">${u.status}</span></h2>
        <p class="sub">UID ${formatUid(u.uid)} · VIP ${u.vip} · 推荐码 ${escapeHtml(u.code || '无')}</p>
        <div class="detail-metrics">
            <div>历史累计佣金<b>${money(u.total)}</b></div>
            <div>本期佣金<b>${u.missing ? '待补齐' : money(u.current)}</b></div>
            <div>本期交易量<b>${u.missing ? '待补齐' : money(u.current_volume)}</b></div>
            <div>较上期佣金变化<b>${u.missing ? '待观察' : change(u.delta)}</b></div>
        </div>
        <div class="facts">
            <div>注册时间 <b>${escapeHtml(u.reg || '-')}</b></div>
            <div>首笔交易 <b>${escapeHtml(u.trade || '-')}</b></div>
            <div>现货返点 <b>${escapeHtml(u.spot)}</b></div>
            <div>合约返点 <b>${escapeHtml(u.futures)}</b></div>
        </div>
        <div class="reason">${reasonText}</div>
        <h3 style="font-size:13px;margin:16px 0 8px">这次联系，记录什么？</h3>
        <label for="careModalNote">跟进记录与客户诉求</label>
        <textarea id="careModalNote" placeholder="例如：最近暂缓策略，约好下周再了解进展…">${escapeHtml(u.latest_log?.note || '')}</textarea>
        <label for="careModalDate">下次联系（可选）</label>
        <input id="careModalDate" type="date" value="${escapeHtml(u.latest_log?.followup_date || '')}">
        <div class="dialog-actions">
            <button class="button" id="saveCareFollowupBtn" data-uid="${u.uid}">保存跟进记录</button>
            <button class="button secondary" id="closeCareModalBtn">稍后再看</button>
        </div>
        <div class="amount-sub" style="margin-top:12px">自动持久化保存至本地 SQLite 数据库，并同步更新客户档案。</div>
    `;

    modal.showModal();
}

function openRulesModal() {
    const modal = $('#rulesModal');
    if (!modal) return;
    modal.innerHTML = `
        <button class="close" id="closeRulesModal" aria-label="关闭">×</button>
        <div class="eyebrow">HOW WE IDENTIFY</div>
        <h2>为什么是这些客户？</h2>
        <p class="sub">基于全周期历史快照与近期时序对比的客户识别逻辑。</p>
        <ul class="rule-list">
            <li><b>近期状态判断</b>本期与上一个等长周期比较。下降 ≥ 80% 或本期为 0 且历史总贡献 ≥ 50U 为沉睡候选；下降 ≥ 30% 为降温；新注册或近期首交易列入新星培育；平稳或上升为活跃关怀。</li>
            <li><b>数据待补齐</b>本期或对比期缺失时不判断活跃状态。缺数据的客户不判沉睡；点击回补按钮补缺，已有有效数据秒级跳过。</li>
            <li><b>周期切换</b>支持 7 / 15 / 30 / 90 / 180 / 365 滚动日对比。历史累计佣金固定，近期贡献和状态随周期变化。</li>
            <li><b>关怀与效果评估</b>VIP 升级与大户动态智能提取关怀信号。跟进记录持久化保存于数据库，回暖效果等下一完整周期真实数据评估。</li>
        </ul>
    `;
    modal.showModal();
}

function exportCareList() {
    const careData = cache.care;
    if (!careData) return;
    const { list } = getCareFilteredCustomers();
    const cell = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
    const rows = [
        ['UID', '备注名', '推荐码', 'VIP等级', '状态', '历史总返佣', '本期返佣', '上期返佣', '较上期变动(%)', '注册时间', '最近跟进记录'],
        ...list.map(u => [
            state.hideSensitive ? maskUid(u.uid) : u.uid,
            state.hideSensitive ? maskName(u.name) : u.name,
            u.code,
            u.vip,
            u.status,
            state.hideSensitive ? maskNumber(u.total.toFixed(2)) : u.total.toFixed(2),
            u.missing ? '数据待补齐' : (state.hideSensitive ? maskNumber(u.current.toFixed(2)) : u.current.toFixed(2)),
            u.missing ? '数据待补齐' : (state.hideSensitive ? maskNumber(u.previous.toFixed(2)) : u.previous.toFixed(2)),
            u.missing ? '—' : (u.delta > 0 ? '+' : '') + u.delta.toFixed(1) + '%',
            u.reg,
            state.hideSensitive ? formatText(u.latest_log?.note || '暂无') : (u.latest_log?.note || '暂无')
        ])
    ];

    const csvContent = '\uFEFF' + rows.map(r => r.map(cell).join(',')).join('\r\n');
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `唤醒与关怀名单_${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`已成功导出 ${list.length} 位客户名单`);
}

function imports() {
    const imp = cache.imports || {};
    const batches = imp.batches || [];

    return `
        <div class="notice">真实数据接入：支持流式解析 CSV 导出客户档案，并向 SAPI 发起限速回补任务。</div>
        <section class="panel">
            <div class="panel-head"><h2>数据同步与回补控制台</h2><span class="sort">SAPI 限速任务</span></div>
            <div class="import-zone">
                <div class="big">⇋</div>
                <h3>客户档案与 SAPI 异步时序回补控制台</h3>
                <p>单线程安全受控调用（间隔 250ms/次）。支持断点续传、失败退避与随时取消。</p>
                <div style="display:flex;gap:10px;justify-content:center;margin-top:16px;flex-wrap:wrap">
                    <input type="file" id="csvFileInput" accept=".csv" style="display:none">
                    <button class="button" id="uploadCsvTriggerBtn" style="background:#24866c;border-color:#24866c">📁 上传新 CSV</button>
                    <button class="button" id="updateCsvBtn">扫描本地 CSV</button>
                    <button class="button secondary" id="startDailySync7">回补近 7 天日级趋势</button>
                    <button class="button secondary" id="startDailySync30">回补近 30 天日级趋势</button>
                    <button class="button secondary" id="startDailySync90">回补近 3 个月日级趋势 (90天)</button>
                    <button class="button secondary" id="syncHalfYearBtn">回补近半年日级趋势 (180天)</button>
                    <button class="button secondary" id="syncOneYearBtn">回补近一年日级趋势 (365天)</button>
                </div>
                <p style="margin:14px auto 0;font-size:11px;color:#8590a2;text-align:center;max-width:680px;line-height:1.6">
                    支持<b>直接点击「📁 上传新 CSV」</b>或<b>将文件拖入此区域</b>，也可以把 CSV 放入代码目录后点击<b>「扫描本地 CSV」</b>。<br>
                    系统会自动增量识别新注册 UID 并自动拉取最近 3 个月各周期指标，已有客户备注及重点关注状态完整保留。
                </p>
                <div id="syncJobStatusBox" style="margin-top:16px;font-size:12px;color:#7d8796"></div>
            </div>
        </section>
        <section class="panel" style="margin-top:20px">
            <div class="panel-head" style="padding-bottom:20px"><h2>本地导入批次记录</h2><span class="sort">${batches.length} 个批次</span></div>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>批次 ID</th><th>文件来源</th><th>类型</th><th>导入时间</th><th>识别客户</th><th>子账户</th><th>摘要</th></tr></thead>
                    <tbody>
                        ${batches.map(b => `
                            <tr>
                                <td><code>${b.batch_id}</code></td>
                                <td>${escapeHtml(b.file_name)}</td>
                                <td><span class="pill">${b.source_type}</span></td>
                                <td>${(b.imported_at || '').slice(0, 19).replace('T', ' ')}</td>
                                <td>${b.unique_uids} 人</td>
                                <td>${b.subaccount_count} 个</td>
                                <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(b.summary || '-')}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
                ${batches.length ? '' : '<div class="empty">暂无导入批次记录。</div>'}
            </div>
        </section>
    `;
}

const extraViews = {};
const titles = {
    overview: ['返佣总览', '汇总客户返佣，了解真实行为变化，找到联系与激活的时机。'],
    customers: ['客户中心', '从每一份真实贡献，了解客户的近期动态。'],
    followups: ['唤醒与关怀', '找到值得联系的人，也照顾好正在同行的人。'],
    imports: ['数据导入与同步', '管理客户档案、导入 CSV 与执行底层 API 时序回补。'],
};

async function reloadCurrentPageData() {
    if (state.page === 'overview') {
        cache.overview = await api(`/api/dashboard/overview?days=${state.days}&excludeTop=${state.excludeTop}&referenceEnd=${state.referenceEnd}`);
    } else if (state.page === 'customers') {
        cache.customers = await api(`/api/dashboard/customers?days=${state.days}&tab=${state.tab}&search=${encodeURIComponent(state.search)}&sort=${state.sort}&referenceEnd=${state.referenceEnd}`);
    } else if (state.page === 'followups') {
        cache.care = await api(`/api/dashboard/care?days=${state.days}&referenceEnd=${state.referenceEnd}`);
    } else if (state.page === 'imports') {
        cache.imports = await api('/api/dashboard/imports');
    } else if (state.page === 'earnings' || state.page === 'dynamics') {
        if (extraViews[state.page + 'Load']) {
            await extraViews[state.page + 'Load']();
        }
    }
}

async function render() {

    const title = titles[state.page] || [state.page, ''];
    if ($('#pageTitle')) $('#pageTitle').textContent = title[0];
    if ($('#crumb')) $('#crumb').textContent = title[0];
    if ($('#pageSubtitle')) $('#pageSubtitle').textContent = title[1];
    const eyebrowEl = $('.eyebrow');
    if (eyebrowEl) {
        eyebrowEl.textContent = state.page === 'followups' ? 'CARE BUILDS LASTING RELATIONSHIPS' : 'UNDERSTAND YOUR CUSTOMERS';
    }

    if ($('#dateLabel')) {
        if (state.page === 'followups' && cache.care?.period) {
            $('#dateLabel').textContent = `${cache.care.period.label} · 对比前 ${state.days} 天`;
        } else if (state.page === 'earnings' && cache.insights?.period) {
            $('#dateLabel').textContent = cache.insights.period.label;
        } else if (cache.overview?.period) {
            $('#dateLabel').textContent = cache.overview.period.label;
        }
    }

    document.querySelectorAll('nav [data-page]').forEach(b => b.classList.toggle('active', b.dataset.page === state.page));
    document.querySelectorAll('[data-days]').forEach(b => b.classList.toggle('selected', +b.dataset.days === state.days));
    if ($('#periods')) $('#periods').hidden = (state.page === 'imports');
    if ($('#dateLabel')) $('#dateLabel').hidden = (state.page === 'imports');

    const maskBtn = $('#toggleMaskBtn');
    if (maskBtn) {
        maskBtn.classList.toggle('active', !!state.hideSensitive);
        const iconEl = maskBtn.querySelector('.mask-icon');
        const textEl = maskBtn.querySelector('.mask-text');
        if (iconEl) iconEl.textContent = state.hideSensitive ? '🔒' : '👁';
        if (textEl) textEl.textContent = state.hideSensitive ? '已隐藏敏感信息' : '隐藏敏感信息';
        maskBtn.title = state.hideSensitive ? '点击显示敏感信息' : '点击隐藏敏感信息';
    }

    const viewFn = { overview, customers, followups, imports, ...extraViews }[state.page] || overview;
    const content = $('#pageContent');
    if (content) content.innerHTML = viewFn();

    if (state.page === 'overview') {
        bindLinkedChartEvents();
    }
}

function getOrCreateTooltip() {
    let el = document.getElementById('chartTooltip');
    if (!el) {
        el = document.createElement('div');
        el.id = 'chartTooltip';
        document.body.appendChild(el);
    }
    return el;
}

function bindLinkedChartEvents() {
    const panel = document.getElementById('linkedTrendsPanel');
    if (!panel) return;

    const t = cache.overview?.trends;
    if (!t || !t.has_daily_data) return;

    const dates = t.dates || [];
    const fullDates = t.full_dates || [];
    const commVals = t.current_commission || [];
    const volVals = t.current_volume || [];
    const prevCommVals = t.previous_commission || [];
    const prevVolVals = t.previous_volume || [];
    const count = Math.max(commVals.length, volVals.length, dates.length);
    if (count <= 1) return;

    const hasPrevComm = prevCommVals.some(v => v > 0);
    const hasPrevVol = prevVolVals.some(v => v > 0);
    const hasPrev = hasPrevComm || hasPrevVol;
    const maxComm = Math.max(...commVals, ...(hasPrevComm ? prevCommVals : [0]), 1) * 1.15;
    const maxVol = Math.max(...volVals, ...(hasPrevVol ? prevVolVals : [0]), 1) * 1.15;

    const chartBlock = panel.querySelector('.linked-chart');
    if (!chartBlock) return;
    const svgEl = chartBlock.querySelector('svg');
    if (!svgEl) return;

    const tooltip = getOrCreateTooltip();

    function handleMove(e) {
        const rect = svgEl.getBoundingClientRect();
        if (rect.width <= 0) return;

        // ViewBox is 760 x 260. Plot x is 55 to 705 (width 650)
        const scaleX = 760 / rect.width;
        const svgX = (e.clientX - rect.left) * scaleX;
        const clampedX = Math.max(55, Math.min(705, svgX));
        const step = 650 / (count - 1);
        const i = Math.max(0, Math.min(count - 1, Math.round((clampedX - 55) / step)));
        const targetX = 55 + i * step;

        const commVal = commVals[i] || 0;
        const volVal = volVals[i] || 0;
        const prevCommVal = prevCommVals[i] || 0;
        const prevVolVal = prevVolVals[i] || 0;

        // 1. Move cursor
        const cursor = svgEl.querySelector('.linked-cursor');
        if (cursor) {
            cursor.setAttribute('x1', targetX.toFixed(1));
            cursor.setAttribute('x2', targetX.toFixed(1));
            cursor.setAttribute('visibility', 'visible');
        }

        // 2. Position Markers
        // Current Commission (Gold)
        const currCommMarker = svgEl.querySelector('.linked-marker.current-comm');
        if (currCommMarker && commVals.length > i) {
            const yComm = 210 - (Math.max(0, commVal) / maxComm) * 185;
            currCommMarker.setAttribute('cx', targetX.toFixed(1));
            currCommMarker.setAttribute('cy', yComm.toFixed(1));
            currCommMarker.setAttribute('visibility', 'visible');
        }

        // Previous Commission (Gold Dashed/Hollow)
        const prevCommMarker = svgEl.querySelector('.linked-marker.prev-comm');
        if (prevCommMarker) {
            if (hasPrevComm && prevCommVals.length > i && prevCommVal > 0) {
                const yPrev = 210 - (Math.max(0, prevCommVal) / maxComm) * 185;
                prevCommMarker.setAttribute('cx', targetX.toFixed(1));
                prevCommMarker.setAttribute('cy', yPrev.toFixed(1));
                prevCommMarker.setAttribute('visibility', 'visible');
            } else {
                prevCommMarker.setAttribute('visibility', 'hidden');
            }
        }

        // Current Volume (Blue)
        const currVolMarker = svgEl.querySelector('.linked-marker.current-vol');
        if (currVolMarker && volVals.length > i) {
            const yVol = 210 - (Math.max(0, volVal) / maxVol) * 185;
            currVolMarker.setAttribute('cx', targetX.toFixed(1));
            currVolMarker.setAttribute('cy', yVol.toFixed(1));
            currVolMarker.setAttribute('visibility', 'visible');
        }

        // Previous Volume (Blue Dashed/Hollow)
        const prevVolMarker = svgEl.querySelector('.linked-marker.prev-vol');
        if (prevVolMarker) {
            if (hasPrevVol && prevVolVals.length > i && prevVolVal > 0) {
                const yPrev = 210 - (Math.max(0, prevVolVal) / maxVol) * 185;
                prevVolMarker.setAttribute('cx', targetX.toFixed(1));
                prevVolMarker.setAttribute('cy', yPrev.toFixed(1));
                prevVolMarker.setAttribute('visibility', 'visible');
            } else {
                prevVolMarker.setAttribute('visibility', 'hidden');
            }
        }

        const isDaySynced = (!t.synced_mask || t.synced_mask[i] !== false);
        const dateStr = fullDates[i] || dates[i];

        // 3. Update Heading readout
        const valCommEl = document.getElementById('value-commission');
        if (valCommEl) {
            if (isDaySynced) {
                let commHtml = `<span class="readout-date">${dates[i]}</span>: <strong class="readout-val gold">${money(commVal)}</strong>`;
                if (prevCommVal > 0) {
                    const diff = ((commVal - prevCommVal) / prevCommVal) * 100;
                    commHtml += ` <span class="readout-diff ${diff >= 0 ? 'up' : 'down'}">(${diff >= 0 ? '+' : ''}${diff.toFixed(1)}%)</span>`;
                }
                valCommEl.innerHTML = commHtml;
            } else {
                valCommEl.innerHTML = `<span class="readout-date">${dates[i]}</span>: <span class="muted" style="font-size:11px;color:#d97706">未回补日级</span>`;
            }
        }

        const valVolEl = document.getElementById('value-volume');
        if (valVolEl) {
            if (isDaySynced) {
                let volHtml = `<span class="readout-date">${dates[i]}</span>: <strong class="readout-val blue">${money(volVal)}</strong>`;
                if (prevVolVal > 0) {
                    const diff = ((volVal - prevVolVal) / prevVolVal) * 100;
                    volHtml += ` <span class="readout-diff ${diff >= 0 ? 'up' : 'down'}">(${diff >= 0 ? '+' : ''}${diff.toFixed(1)}%)</span>`;
                }
                valVolEl.innerHTML = volHtml;
            } else {
                valVolEl.innerHTML = `<span class="readout-date">${dates[i]}</span>: <span class="muted" style="font-size:11px;color:#d97706">未回补日级</span>`;
            }
        }

        // 4. Update #linkedReadout bar
        const readoutEl = document.getElementById('linkedReadout');
        if (readoutEl) {
            if (isDaySynced) {
                let txt = `📅 <strong>${dateStr}</strong> · 返佣: <strong style="color:#b08b32">${money(commVal)}</strong>`;
                if (prevCommVal > 0) {
                    txt += ` (${change(((commVal - prevCommVal) / prevCommVal) * 100)})`;
                }
                txt += ` · 交易额: <strong style="color:#2563eb">${money(volVal)}</strong>`;
                if (prevVolVal > 0) {
                    txt += ` (${change(((volVal - prevVolVal) / prevVolVal) * 100)})`;
                }
                readoutEl.innerHTML = txt;
            } else {
                readoutEl.innerHTML = `📅 <strong>${dateStr}</strong> · <span style="color:#d97706">⚠️ 该日期日级时序尚未回补，点击右上角按钮可补齐历史趋势</span>`;
            }
        }

        // 5. Tooltip
        let ttBody = '';
        if (isDaySynced) {
            ttBody = `
                <div class="tt-section">
                    <div class="tt-row">
                        <span class="tt-label"><i class="legend-line solid gold mini"></i>本期返佣 (左轴):</span>
                        <span class="tt-value gold">${money(commVal)}</span>
                    </div>
                    ${prevCommVal > 0 ? `
                        <div class="tt-row muted">
                            <span class="tt-label"><i class="legend-line dashed gold mini"></i>上期同日:</span>
                            <span>${money(prevCommVal)} <b class="${commVal >= prevCommVal ? 'up' : 'down'}">(${commVal >= prevCommVal ? '+' : ''}${(((commVal - prevCommVal) / prevCommVal) * 100).toFixed(1)}%)</b></span>
                        </div>
                    ` : (hasPrevComm ? `<div class="tt-row muted"><span class="tt-label"><i class="legend-line dashed gold mini"></i>上期同日:</span><span>0.00</span></div>` : '')}
                </div>
                <div class="tt-divider"></div>
                <div class="tt-section">
                    <div class="tt-row">
                        <span class="tt-label"><i class="legend-line solid blue mini"></i>本期交易额 (右轴):</span>
                        <span class="tt-value blue">${money(volVal)}</span>
                    </div>
                    ${prevVolVal > 0 ? `
                        <div class="tt-row muted">
                            <span class="tt-label"><i class="legend-line dashed blue mini"></i>上期同日:</span>
                            <span>${money(prevVolVal)} <b class="${volVal >= prevVolVal ? 'up' : 'down'}">(${volVal >= prevVolVal ? '+' : ''}${(((volVal - prevVolVal) / prevVolVal) * 100).toFixed(1)}%)</b></span>
                        </div>
                    ` : (hasPrevVol ? `<div class="tt-row muted"><span class="tt-label"><i class="legend-line dashed blue mini"></i>上期同日:</span><span>0.00</span></div>` : '')}
                </div>
            `;
        } else {
            ttBody = `
                <div class="tt-row" style="color:#b45309;padding:4px 0">
                    <span class="tt-label">⚠️ 状态:</span>
                    <span class="tt-value" style="font-weight:600">日级数据待回补</span>
                </div>
                <div style="font-size:11px;color:#8590a2;line-height:1.4">该日期历史日级数据尚未回补</div>
            `;
        }

        tooltip.innerHTML = `
            <div class="tt-header">
                <span class="tt-date">${dateStr}</span>
                <span class="tt-idx">第 ${i + 1}/${count} 天</span>
            </div>
            <div class="tt-body">${ttBody}</div>
        `;

        tooltip.style.display = 'block';
        const ttWidth = tooltip.offsetWidth || 230;
        let leftPos = e.clientX + 16;
        if (leftPos + ttWidth > window.innerWidth - 10) {
            leftPos = e.clientX - ttWidth - 16;
        }
        tooltip.style.left = `${leftPos}px`;
        tooltip.style.top = `${e.clientY}px`;
    }

    function handleLeave() {
        svgEl.querySelectorAll('.linked-cursor, .linked-marker').forEach(el => {
            el.setAttribute('visibility', 'hidden');
        });
        tooltip.style.display = 'none';

        const ovMetrics = cache.overview?.metrics || {};
        const valCommEl = document.getElementById('value-commission');
        if (valCommEl) {
            valCommEl.innerHTML = `<span class="readout-date">本期合计</span>: <strong class="readout-val gold">${money(ovMetrics.total_commission || 0)}</strong>`;
        }
        const valVolEl = document.getElementById('value-volume');
        if (valVolEl) {
            valVolEl.innerHTML = `<span class="readout-date">本期合计</span>: <strong class="readout-val blue">${money(ovMetrics.total_volume || 0)}</strong>`;
        }

        const readoutEl = document.getElementById('linkedReadout');
        if (readoutEl && t.notice) {
            readoutEl.innerHTML = t.notice;
        }
    }

    chartBlock.addEventListener('mousemove', handleMove);
    chartBlock.addEventListener('mouseleave', handleLeave);
}

async function navigate(page) {
    state.page = page;
    await reloadCurrentPageData();
    await render();
}

async function openClient(uid) {
    openedUid = uid;
    focusReturn = document.activeElement;
    $('#overlay').hidden = false;
    $('#drawer').hidden = false;
    document.body.style.overflow = 'hidden';
    $('#drawer').innerHTML = '<div class="drawer-top"><span>客户详情</span><button class="close" id="closeDrawer">×</button></div><div class="empty">正在加载客户资料…</div>';

    const detail = await api(`/api/dashboard/customers/${uid}?days=${state.days}&referenceEnd=${state.referenceEnd}`);
    if (!detail) {
        $('#drawer').innerHTML = '<div class="empty">客户资料未找到。</div>';
        return;
    }
    cache.currentDetail = detail;

    const p = detail.period || {};
    const subs = detail.subaccounts || [];
    const followupsList = detail.followups || [];

    const regDateStr = detail.reg_time ? new Date(detail.reg_time).toISOString().slice(0, 10) : '-';
    const kycDateStr = detail.kyc_time ? new Date(detail.kyc_time).toISOString().slice(0, 10) : '-';
    const firstTradeDateStr = detail.first_trade_time ? new Date(detail.first_trade_time).toISOString().slice(0, 10) : '-';

    $('#drawer').innerHTML = `
        <div class="drawer-top">
            <span>客户详情 / 真实数据</span>
            <button class="close" id="closeDrawer" aria-label="关闭详情">×</button>
        </div>
        <h2 id="drawerTitle">${escapeHtml(formatCustomerName(detail.name))}</h2>
        <p class="sub">UID ${formatUid(detail.user_id)}　·　VIP ${detail.vip_level}　·　${escapeHtml(detail.referral_code || '无推荐码')}</p>
        <div class="drawer-actions">
            <button class="button secondary" id="toggleStar">${detail.is_starred ? '★ 已重点关注' : '☆ 加入重点关注'}</button>
        </div>
        <div class="metrics">
            ${metric('本期返佣', (!isNaN(Number(p.commission)) ? money(p.commission) : (p.commission || '-')), 'USDT', change(p.delta_pct))}
            ${metric('本期交易额', (!isNaN(Number(p.volume)) ? money(p.volume) : (p.volume || '-')), 'USDT', '来自底层 API')}
        </div>
        <section class="detail-block">
            <h3>客户基础档案</h3>
            <div class="detail-row"><span>注册时间</span><b>${regDateStr}</b></div>
            <div class="detail-row"><span>KYC 完成</span><b>${kycDateStr}</b></div>
            <div class="detail-row"><span>首笔交易</span><b>${firstTradeDateStr}</b></div>
            <div class="detail-row"><span>平台现货返佣率</span><b>${detail.spot_rebate_rate || '-'}</b></div>
            <div class="detail-row"><span>平台合约返佣率</span><b>${detail.futures_rebate_rate || '-'}</b></div>
            <div class="detail-row"><span>全生命周期总返佣</span><b>${detail.all_time_commission ? money(detail.all_time_commission) + ' USDT' : '-'}</b></div>
            <div class="detail-row"><span>全生命周期总交易额</span><b>${detail.all_time_trade_vol ? money(detail.all_time_trade_vol) + ' USDT' : '-'}</b></div>
        </section>
        <section class="detail-block">
            <h3>已绑定子账户 (${subs.length} 个)</h3>
            <p class="sub" style="margin-bottom:10px">来源于已导入 CSV 映射。SAPI 无法穿透查询子账户明细。</p>
            ${subs.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap">${subs.map(s => `<span class="pill">子账户 ${formatUid(s)}</span>`).join('')}</div>` : '<p class="muted">未发现名下绑定子账户。</p>'}
        </section>
        <section class="detail-block">
            <h3>沟通跟进与备忘</h3>
            <label for="drawerNote" class="sub">新增沟通记录</label>
            <textarea id="drawerNote" placeholder="记录客户近期反馈、服务诉求或沟通要点…">${escapeHtml(detail.manual_note || '')}</textarea>
            <div class="detail-row">
                <label for="nextDate">下次跟进日期</label>
                <input id="nextDate" type="date" value="${escapeHtml(detail.next_followup_date || '')}">
            </div>
            <button class="button" id="saveNote" style="margin-top:16px;width:100%">保存跟进记录</button>
            ${followupsList.length ? `
                <div style="margin-top:20px;border-top:1px solid #edf0f3;padding-top:14px">
                    <h4 style="font-size:12px;color:#8590a2;margin:0 0 10px">历史跟进记录 (${followupsList.length}条)</h4>
                    ${followupsList.map(f => `<div style="font-size:11px;margin-bottom:8px;padding-bottom:8px;border-bottom:1px dashed #edf0f3"><span class="muted">${(f.created_at || '').slice(0, 16).replace('T', ' ')}</span><p style="margin:4px 0 0;color:#505f79">${escapeHtml(formatText(f.note))}</p></div>`).join('')}
                </div>
            ` : ''}
        </section>
    `;
    $('#closeDrawer').focus();
}

function closeDrawer() {
    $('#drawer').hidden = true;
    $('#overlay').hidden = true;
    document.body.style.overflow = '';
    openedUid = null;
    focusReturn?.focus();
}

document.addEventListener('click', async e => {
    const b = e.target.closest('button, tr[data-client-uid]');
    if (!b) return;

    if (b.id === 'toggleMaskBtn' || b.closest('#toggleMaskBtn')) {
        state.hideSensitive = !state.hideSensitive;
        try {
            localStorage.setItem('hideSensitive', state.hideSensitive ? '1' : '0');
        } catch (_) {}
        await render();
        return;
    }

    // Care & Re-engagement events
    if (b.dataset.careTab) {
        careState.tab = b.dataset.careTab;
        render();
        return;
    }

    if (b.dataset.careFilter) {
        careState.filter = b.dataset.careFilter;
        careState.page = 0;
        careState.tab = 'workbench';
        render();
        return;
    }

    if (b.dataset.careUid) {
        openCareModal(b.dataset.careUid);
        return;
    }

    if (b.dataset.filterCode) {
        careState.code = b.dataset.filterCode;
        careState.filter = '全部';
        careState.tab = 'workbench';
        careState.page = 0;
        render();
        return;
    }

    if (b.id === 'careSyncBtn') {
        await syncMissingCustomers();
        return;
    }

    if (b.id === 'openRulesBtn') {
        openRulesModal();
        return;
    }

    if (b.id === 'closeRulesModal') {
        $('#rulesModal')?.close();
        return;
    }

    if (b.id === 'closeCareModal' || b.id === 'closeCareModalBtn') {
        $('#careModal')?.close();
        return;
    }

    if (b.id === 'exportCareBtn') {
        exportCareList();
        return;
    }

    if (b.id === 'carePrevPage') {
        if (careState.page > 0) {
            careState.page--;
            renderCareRows();
        }
        return;
    }

    if (b.id === 'careNextPage') {
        const { pages } = getCareFilteredCustomers();
        if (careState.page < pages - 1) {
            careState.page++;
            renderCareRows();
        }
        return;
    }

    if (b.id === 'saveCareFollowupBtn') {
        const uid = b.dataset.uid;
        const note = ($('#careModalNote')?.value || '').trim();
        const nextDate = $('#careModalDate')?.value || null;
        if (!note) {
            toast('请先填写这次联系的记录');
            $('#careModalNote')?.focus();
            return;
        }
        const res = await api(`/api/dashboard/customers/${uid}/followup`, {
            method: 'POST',
            body: JSON.stringify({ note, next_followup_date: nextDate }),
        });
        if (res) {
            toast('跟进记录已持久化保存');
            $('#careModal')?.close();
            await reloadCurrentPageData();
            await render();
        }
        return;
    }

    if (b.id === 'syncMissingCustomers') { await syncMissingCustomers(); return; }

    if (b.dataset.page) {
        navigate(b.dataset.page);
        return;
    }

    if (b.dataset.days) {
        state.days = +b.dataset.days;
        await reloadCurrentPageData();
        await render();
        return;
    }

    if (b.dataset.starUid !== undefined) {
        const targetUid = b.dataset.starUid;
        const res = await api(`/api/dashboard/customers/${targetUid}/star`, { method: 'POST' });
        if (res) {
            toast(res.is_starred ? '★ 已加入重点关注' : '☆ 已取消重点关注');
            await reloadCurrentPageData();
            await render();
            if (openedUid === targetUid) {
                await openClient(targetUid);
            }
        }
        return;
    }

    if (b.dataset.clientUid !== undefined) {
        openClient(b.dataset.clientUid);
        return;
    }

    if (b.dataset.filter) {
        state.tab = b.dataset.filter;
        navigate('customers');
        return;
    }

    if (b.dataset.tab) {
        state.tab = b.dataset.tab;
        await reloadCurrentPageData();
        await render();
        return;
    }

    if (b.dataset.excludeTop !== undefined) {
        state.excludeTop = +b.dataset.excludeTop;
        await reloadCurrentPageData();
        await render();
        return;
    }

    if (b.id === 'closeDrawer') closeDrawer();

    if (b.id === 'toggleStar' && openedUid) {
        const res = await api(`/api/dashboard/customers/${openedUid}/star`, { method: 'POST' });
        if (res) {
            toast(res.is_starred ? '★ 已加入重点关注' : '☆ 已取消重点关注');
            await reloadCurrentPageData();
            await render();
            await openClient(openedUid);
        }
    }

    if (b.id === 'saveNote' && openedUid) {
        const note = $('#drawerNote').value;
        const nextDate = $('#nextDate').value;
        const res = await api(`/api/dashboard/customers/${openedUid}/followup`, {
            method: 'POST',
            body: JSON.stringify({ note, next_followup_date: nextDate || null }),
        });
        if (res) {
            toast('沟通与跟进记录已持久化保存');
            await reloadCurrentPageData();
            await render();
            await openClient(openedUid);
        }
    }

    if (b.id === 'uploadCsvTriggerBtn') {
        const inp = document.getElementById('csvFileInput');
        if (inp) {
            inp.value = '';
            inp.click();
        }
        return;
    }

    if (b.id === 'updateCsvBtn' || b.id === 'importDefaultsBtn') {
        const res = await api('/api/dashboard/imports/update_csv', { method: 'POST' });
        if (res) {
            toast(res.message);
            if (res.auto_sync_job_id) {
                pollSyncStatus();
            }
            await reloadCurrentPageData();
            await render();
        }
    }

    if (b.id === 'sync90DaysBtn') {
        const res = await api('/api/dashboard/sync/start', {
            method: 'POST',
            body: JSON.stringify({ scope: 'all', days: 90, granularity: 'period' }),
        });
        if (res) {
            toast(`已启动近 3 个月 (90天) 全员数据同步 (任务ID: ${res.job_id})`);
            pollSyncStatus();
        }
    }

    if (b.id === 'startDailySync7') {
        const res = await api('/api/dashboard/sync/start', {
            method: 'POST',
            body: JSON.stringify({ scope: 'all', days: 7, granularity: 'daily' }),
        });
        if (res) {
            toast(res.message || `已启动近 7 天日级时序回补 (任务ID: ${res.job_id})`);
            pollSyncStatus();
        }
    }

    if (b.id === 'startDailySync' || b.id === 'startDailySync30') {
        const res = await api('/api/dashboard/sync/start', {
            method: 'POST',
            body: JSON.stringify({ scope: 'all', days: 30, granularity: 'daily' }),
        });
        if (res) {
            toast(res.message || `已启动近 30 天日级时序回补 (任务ID: ${res.job_id})`);
            pollSyncStatus();
        }
    }

    if (b.id === 'startDailySync90') {
        const res = await api('/api/dashboard/sync/start', {
            method: 'POST',
            body: JSON.stringify({ scope: 'all', days: 90, granularity: 'daily' }),
        });
        if (res) {
            toast(res.message || `已启动近 3 个月 (90天) 日级时序回补 (任务ID: ${res.job_id})`);
            pollSyncStatus();
        }
    }

    if (b.id === 'syncHalfYearBtn' || b.id === 'syncHalfYearDailyBtn') {
        const res = await api('/api/dashboard/sync/start', {
            method: 'POST',
            body: JSON.stringify({ scope: 'all', days: 180, granularity: 'daily' }),
        });
        if (res) {
            toast(res.message || `已启动近半年 (180天) 日级时序回补 (任务ID: ${res.job_id})`);
            pollSyncStatus();
        }
    }

    if (b.id === 'syncOneYearBtn' || b.id === 'syncOneYearDailyBtn') {
        const res = await api('/api/dashboard/sync/start', {
            method: 'POST',
            body: JSON.stringify({ scope: 'all', days: 365, granularity: 'daily' }),
        });
        if (res) {
            toast(res.message || `已启动近一年 (365天) 日级时序回补 (任务ID: ${res.job_id})`);
            pollSyncStatus();
        }
    }

    if (b.id === 'syncTrendPeriodBtn') {
        const targetDays = b.dataset.days ? +b.dataset.days : state.days;
        const res = await api('/api/dashboard/sync/start', {
            method: 'POST',
            body: JSON.stringify({ scope: 'all', days: targetDays, granularity: 'daily' }),
        });
        if (res) {
            toast(res.message || `已启动近 ${targetDays} 天日级时序回补 (任务ID: ${res.job_id})`);
            pollSyncStatus();
        }
    }

    if (b.id === 'triggerSyncFromTrend') {
        navigate('imports');
    }
});

async function pollSyncStatus() {
    const res = await api('/api/dashboard/sync/status');
    const j = res?.latest_job;
    if (!j) return;

    const box = $('#syncJobStatusBox');
    if (box) {
        box.innerHTML = `当前任务 [${j.id}]: 状态 <b>${j.status}</b> | 进度: ${j.processed_items}/${j.total_items} (成功: ${j.success_items}, 失败: ${j.failed_items}) ${j.message ? '· ' + escapeHtml(j.message) : ''}`;
    }

    if (j.status === 'running') {
        const pct = Math.round((j.processed_items / Math.max(j.total_items, 1)) * 100);
        const readout = $('#linkedReadout');
        if (readout) {
            readout.innerHTML = `<span style="color:#b28532">⏳ 正在从币安 SAPI 异步回补日级时序数据（进度: ${j.processed_items}/${j.total_items} · ${pct}%）… 数据持续写入并自动刷新</span>`;
        }
        setTimeout(pollSyncStatus, 2000);
    } else {
        // Job completed or finished
        await reloadCurrentPageData();
        await render();
    }
}

document.addEventListener('change', async e => {
    if (e.target.id === 'sort') {
        state.sort = e.target.value;
        await reloadCurrentPageData();
        await render();
    } else if (e.target.id === 'careCodeFilter') {
        careState.code = e.target.value;
        careState.page = 0;
        renderCareRows();
    } else if (e.target.id === 'careSort') {
        careState.sort = e.target.value;
        careState.page = 0;
        renderCareRows();
    }
});

let searchDebounce;
document.addEventListener('input', e => {
    if (e.target.id === 'search') {
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(async () => {
            state.search = e.target.value;
            await reloadCurrentPageData();
            await render();
            const sEl = $('#search');
            if (sEl) sEl.focus();
        }, 300);
    } else if (e.target.id === 'careSearch') {
        careState.search = e.target.value;
        careState.page = 0;
        renderCareRows();
    }
});

if ($('#overlay')) $('#overlay').addEventListener('click', closeDrawer);
document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && openedUid !== null) closeDrawer();
});

// File Upload & Drag-and-Drop Handler
async function handleCsvUpload(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.csv')) {
        toast('请选择或拖入 .csv 格式文件');
        return;
    }
    toast(`正在上传并解析 ${file.name}…`);
    const formData = new FormData();
    formData.append('file', file);
    try {
        const resp = await fetch('/api/dashboard/imports/upload', {
            method: 'POST',
            body: formData,
        });
        const res = await resp.json();
        if (resp.ok && res.status === 'success') {
            toast(res.message);
            if (res.auto_sync_job_id) {
                pollSyncStatus();
            }
            await reloadCurrentPageData();
            await render();
        } else {
            toast(`上传失败：${res.detail || res.message || '未知错误'}`);
        }
    } catch (err) {
        toast(`上传失败：${err.message || err}`);
    }
}

document.addEventListener('change', async (e) => {
    if (e.target && e.target.id === 'csvFileInput') {
        const file = e.target.files && e.target.files[0];
        await handleCsvUpload(file);
    }
});

document.addEventListener('dragover', (e) => {
    if (e.target && e.target.closest('.import-zone')) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
    }
});

document.addEventListener('drop', async (e) => {
    const zone = e.target && e.target.closest('.import-zone');
    if (zone && e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        e.preventDefault();
        await handleCsvUpload(e.dataTransfer.files[0]);
    }
});

// Initial boot
(async function init() {
    await reloadCurrentPageData();
    await render();
    const res = await api('/api/dashboard/sync/status');
    if (res?.latest_job?.status === 'running') {
        pollSyncStatus();
    }
})();
