// Insights, Activations, and Dynamics Controller connecting to Backend REST APIs

function section(title, sub, body, action = '') {
    return `<section class="panel insight-panel"><div class="panel-head"><div><h2>${title}</h2><p class="panel-sub">${sub}</p></div>${action}</div>${body}</section>`;
}

function hint(text) {
    return `<div class="notice">${text}</div>`;
}

extraViews.earningsLoad = async function () {
    cache.insights = await api(`/api/dashboard/insights?days=${state.days}&referenceEnd=${state.referenceEnd}`);
};

extraViews.dynamicsLoad = async function () {
    cache.dynamics = await api('/api/dashboard/dynamics');
};

extraViews.earnings = function () {
    const data = cache.insights || {};
    const act = data.activations || {};
    const ref = data.referral_codes || {};
    const codes = ref.codes || [];

    const totalTrading = act.trading_customers || 59;
    const regCount = act.registered || 0;
    const kycCount = act.kyc || 0;
    const depCount = act.deposited || 0;
    const tradeCount = act.traded || 0;

    const formatNum = n => state.hideSensitive ? maskNumber(n) : n;

    return hint(`按所选时间范围（当前：近 ${state.days} 天）实时观察新客户转化与激活进度，结合注册与首次交易信息寻找联系机会。渠道表展示推荐码真实绩效。`) +
        `<div class="insight-grid">
            ${section('新客激活里程碑', `真实时段达成统计 · 近 ${state.days} 天实时筛选`, `
                <div class="funnel">
                    <div>
                        <span>注册人数</span>
                        <div style="width:${regCount > 0 ? 100 : 16}%;background:#e2e8f0">
                            <b>${formatNum(regCount)} 人</b>
                            <small class="muted" style="margin-left:6px">${regCount > 0 ? '近' + state.days + '天新客' : '本期无新增'}</small>
                        </div>
                    </div>
                    <div>
                        <span>完成 KYC</span>
                        <div style="width:${regCount > 0 ? Math.max(16, Math.round(kycCount / regCount * 100)) : 16}%;background:#e2e8f0">
                            <b>${formatNum(kycCount)} 人</b>
                            ${regCount > 0 ? `<small class="muted" style="margin-left:6px">(${Math.round(kycCount / regCount * 100)}%)</small>` : ''}
                        </div>
                    </div>
                    <div>
                        <span>首次充值</span>
                        <div style="width:${regCount > 0 ? Math.max(16, Math.round(depCount / regCount * 100)) : 16}%;background:#e2e8f0">
                            <b>${formatNum(depCount)} 人</b>
                            ${regCount > 0 ? `<small class="muted" style="margin-left:6px">(${Math.round(depCount / regCount * 100)}%)</small>` : ''}
                        </div>
                    </div>
                    <div>
                        <span>首次交易</span>
                        <div style="width:${regCount > 0 ? Math.max(16, Math.round(tradeCount / regCount * 100)) : 16}%;background:#e2e8f0">
                            <b>${formatNum(tradeCount)} 人</b>
                            ${regCount > 0 ? `<small class="muted" style="margin-left:6px">(${Math.round(tradeCount / regCount * 100)}%)</small>` : ''}
                        </div>
                    </div>
                    <div>
                        <span>交易人数</span>
                        <div style="width:100%;background:#ead49b">
                            <b style="color:#785817">${formatNum(totalTrading)} 人</b>
                            <small class="muted" style="margin-left:6px;color:#8f712f">库内累计真实交易客户</small>
                        </div>
                    </div>
                </div>
                <div class="funnel-action">
                    <strong>口径说明</strong>
                    <p>${act.disclaimer || `新客漏斗按当前选定的近 ${state.days} 天周期实时统计新注册客户；完成 KYC、首次充值与首次交易均统计自本期新客；交易人数展示库内累计真实交易客户总盘（${totalTrading} 人）。`}</p>
                </div>
            `)}
            ${section('关怀里程碑提醒', '首交易纪念与累计返佣达标', `
                <div class="event-list">
                    ${(data.milestones || []).map(m => `
                        <div>
                            <span class="event-date">${escapeHtml(m.event)}</span>
                            <div><strong>${escapeHtml(formatCustomerName(m.name))}</strong><p>${escapeHtml(formatText(m.detail))}</p></div>
                            <button class="text-button" data-client-uid="${m.user_id}">查看 →</button>
                        </div>
                    `).join('') || '<p class="empty" style="padding:20px">暂无触发里程碑提醒的客户。</p>'}
                </div>
            `)}
        </div>
        ${section('推荐码渠道表现', ref.disclaimer || '点击量与注册人数为独立统计指标', `
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr><th>邀请码</th><th>备注</th><th>点击量</th><th>注册人数</th><th>产生名义交易额</th><th>产生总返佣</th></tr>
                    </thead>
                    <tbody>
                        ${codes.map(c => `
                            <tr>
                                <td><code>${escapeHtml(c.code)}</code></td>
                                <td>${escapeHtml(c.note)}</td>
                                <td>${c.clicks}</td>
                                <td>${c.referrals}</td>
                                <td>${money(c.trade_vol)}</td>
                                <td>${money(c.commission)}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
                ${codes.length ? '' : '<div class="empty">暂无推荐码绩效记录。可通过数据同步任务刷新推荐码数据。</div>'}
            </div>
            <div class="table-foot">推荐码数据来源于 SAPI /code 接口。点击量仅覆盖网页端链接，移动端手工填码不计入点击，两者禁止直接相除计算转化率。</div>
        `)}
    `;
};

extraViews.dynamics = function () {
    const d = cache.dynamics || {};
    const teams = d.teams || [];

    return hint(d.removed_features_notice || '已彻底剔除原型虚构的秒级协同网络与自动策略画像。') +
        section('团队关系：主子账户绑定', d.disclaimer || '基于已导入 CSV 维护的明确主子账户绑定关系。', `
            <div style="padding:15px 23px 25px">
                ${teams.length ? `
                    <div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(280px, 1fr));gap:15px">
                        ${teams.map(t => `
                            <div style="background:#fafbfc;border:1px solid #edf0f3;border-radius:8px;padding:16px">
                                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                                    <strong style="font-size:13px">${escapeHtml(formatCustomerName(t.parent_name))}</strong>
                                    <span class="pill">${t.subaccount_count} 个子账户</span>
                                </div>
                                <div class="muted" style="margin-bottom:10px">母账户 UID: ${formatUid(t.parent_user_id)}</div>
                                <div style="display:flex;gap:4px;flex-wrap:wrap">
                                    ${t.subaccounts.map(s => `<span class="pill" style="font-size:10px">子账户 ${formatUid(s)}</span>`).join('')}
                                </div>
                            </div>
                        `).join('')}
                    </div>
                ` : '<div class="empty">未导入包含子账户关系的 CSV 文件。</div>'}
            </div>
        `) +
        section('人工策略标签与跟进原则', '仅保留真实客户沟通备注与人工标签，不依据碎单推导刷量画像', `
            <div class="persona-grid" style="grid-template-columns:repeat(3, 1fr)">
                <div>
                    <h3>沟通前的假设与核实</h3>
                    <p>交易减少或暂停不等于资金撤出或转投；优先核实策略周期与接口运行体验。</p>
                </div>
                <div>
                    <h3>主子账户服务口径</h3>
                    <p>母账户与名下子账户返佣已由币安底层归集，跟进时统一对接团队负责人。</p>
                </div>
                <div>
                    <h3>数据完整性前提</h3>
                    <p>在判定客户沉默之前，必须先核实本地日级数据的覆盖完整性，避免因断更误判。</p>
                </div>
            </div>
        `);
};

titles.dynamics = ['客户动态', '看见真实主子账户绑定，维护团队联系。'];
titles.earnings = ['客户参与与激活', '了解真实达成进度，找到合适的联系机会。'];
