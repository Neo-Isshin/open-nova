// ── 全局错误捕获 ──
window.onerror = function(msg, url, line, col, err) {
  document.getElementById('sseStatus').textContent = '❌ JS Error L' + line + ': ' + msg;
  document.getElementById('sseStatus').style.color = '#e53e3e';
  console.error('JS Error:', msg, 'at line', line, col, err);
};
window.addEventListener('unhandledrejection', function(e) {
  document.getElementById('sseStatus').textContent = '❌ Promise: ' + e.reason;
  document.getElementById('sseStatus').style.color = '#e53e3e';
  console.error('Unhandled rejection:', e.reason);
});

// ── Navigation ──
function escapeHtml(text) {
  const esc = t => t ? String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function toggleSection(el) {
  el.classList.toggle('open');
  const items = el.nextElementSibling;
  if (items) items.classList.toggle('open');
}

let modalHistory = [];
let MSGBOX_STATE = { items: [], attentionCount: 0, count: 0 };
let BACKGROUND_TASK_STATE = { activeCount: 0, tasks: [], active: [] };
let backgroundTasksTimer = null;
let HISTORY_BACKFILL_SELECTED_PERIODS = [];
let HISTORY_BACKFILL_PICKER_PERIODS = [];
let HISTORY_BACKFILL_LAST_PLAN = null;
let HISTORY_BACKFILL_LAST_PLAN_KEY = '';
let HISTORY_BACKFILL_LAST_PLAN_PAYLOAD = null;
let RAG_PRODUCTION_SYNC_BUSY = false;
let ACTANARA_DASHBOARD_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Hong_Kong';
let ACTANARA_PIPELINE_LANGUAGE_PROFILE = 'zh';
let ACTANARA_SETTINGS_LOADED = false;
const DASHBOARD_RESTART_COMMAND = 'python3 advanced/dashboard/dashboard_launch_agent.py check --restart';

const DASHBOARD_TEXT = {
  zh: {
    dayUnit: '天',
    dayDataUnit: '天数据',
    weeklyReport: '周报',
    monthlySummary: '月度汇总',
    diaryNavDiary: '日记',
    diaryNavLoading: '点击侧边栏日期加载',
    diaryNavWeeklyOverview: '周报总览',
    diaryNavMonthlyOverview: '月报总览',
    detailedDiary: '详细日记',
    saveChanges: '保存修改',
    exportPrint: '导出',
    exportPrintTitle: '导出 / 打印',
    refreshAssets: '更新资产',
    refreshAssetsTitle: '在后台重建资产投影',
    loadingWeekly: '正在读取周报快照…',
    loadingMonthly: '正在读取月报快照…',
    loadingDiary: '正在读取日记快照…',
    loadingEllipsis: '加载中…',
    loadFailed: '加载失败: ',
    weeklySummary: '每周总结',
    monthlySummarySection: '每月总结',
    generateMonthlySummaryTitle: '生成并保存本月总结快照',
    periodSummary: '周期总结',
    generateSummary: '生成总结',
    generateWeeklySummaryTitle: '生成并保存本周总结快照',
    overview: '周度概览',
    monthlyOverview: '月度概览',
    periodOverview: '周期概览',
    monthlyPulse: '月度脉冲',
    monthlyPulseScale: '规模',
    monthlyPulseRhythm: '节奏',
    monthlyPulseQuality: '效率',
    monthlyPulseReliability: '稳定性',
    timeInvestment: '时间投入',
    usageTrend: '周度使用趋势',
    monthlyUsageTrend: '月度使用趋势',
    periodUsageTrend: '周期使用趋势',
    modelRanking: '模型消耗量排行',
    agentWorkspaceRanking: 'Agent / Workspace 周度排行',
    monthlyAgentWorkspaceRanking: 'Agent / Workspace 月度排行',
    periodAgentWorkspaceRanking: 'Agent / Workspace 排行',
    tokenRank: 'Token 消耗排行',
    messageActivity: '消息活跃度',
    activeDays: '活跃天数',
    ragChange: 'RAG 变动',
    summaryDetails: '总结详情',
    highFrequencyTopics: '高频主题',
    topicSource: '由周报总结 LLM 基于本周期数据提炼；生成或更新总结后刷新。',
    taskCompletion: '任务完成率',
    workloadComparison: '工作强度',
    scheduledJobs: '定时任务',
    knowledgeBase: '知识库',
    weeklyKnowledgePeriod: '本周',
    monthlyKnowledgePeriod: '本月',
    tasksOutcomes: '任务与成果',
    lessons: '教训与经验',
    noTopics: '暂无 LLM 高频主题；请先生成总结快照。',
    snapshotMissingTitle: '该周期的 Foundation 快照缺失',
    snapshotMissingDesc: '页面已停止实时重算，避免长时间卡住。点击按钮后会在后台重新聚合数据，完成后自动刷新本页。',
    diarySnapshotMissingTitle: '该日的 Foundation 快照缺失',
    diarySnapshotMissingDesc: '页面已停止读取 Markdown 回退，避免长时间卡住。点击按钮后会在后台重新聚合当天数据，完成后自动刷新本页。',
    rebuildData: '重新聚合数据',
    source: '来源',
    status: '状态',
    generated: '生成',
    noSummaryPrefix: '暂无总结快照',
    clickGenerateSummary: '，请点击生成总结。',
    sentenceEnd: '。',
    emptySummary: '总结快照为空。',
    highlights: '重点成果',
    retrospective: '复盘提醒',
    strength: '强度',
    mentioned: '提及',
    submitting: '提交中…',
    queued: '排队中…',
    updating: '更新中…',
    generating: '生成中…',
    updateFailed: '更新失败',
    generationFailed: '生成失败',
    assetRefreshFailed: '资产投影更新失败: ',
    summaryRefreshFailed: '总结快照生成失败: ',
    foundationSnapshot: 'Foundation 快照',
    snapshot: '快照',
    snapshotMissing: '快照缺失，请更新资产',
    foundationPage: 'Foundation 页面',
    pageSnapshot: '页面快照',
    pageSnapshotMissing: '页面快照缺失',
    summarySnapshot: '总结快照',
    summarySnapshotMissing: '总结快照缺失',
    weekLabel: (reportId) => `${wrDisplayWeekId(reportId)} 周报`,
    monthlyOverviewTitle: '月报总览',
    monthLabel: (year, month) => `${year} 年 ${month} 月`,
    cacheHitRate: '缓存命中率',
    noData: '暂无数据',
    workspaceUsageNote: '按 AI 资产同源会话归属逻辑统计，低于 10M tokens 的 workspace 不展示。',
    agentFallbackUsageNote: '当前没有可归属的 workspace 数据，暂以逐日日记中的 Agent 汇总展示。',
    dayRatio: (value, total) => `${value}/${total} 天`,
    currentRagTotal: '现有 nova-RAG 总量',
    currentMemoryTotal: '现有 Memory 总量',
    noComparableSnapshot: '无可比较快照',
    noComparablePeriod: '无上一周期快照',
    snapshotDelta: (label, from, to) => `${label}快照增量（${from} 基线 → ${to}）`,
    periodDelta: (label) => `${label}增量`,
    countItems: '条',
    fileItems: '文件',
    activeSessions: '活跃 Sessions',
    accumulated: '累计',
    cronJobsLabel: '定时任务',
    active: '活跃',
    completed: '已完成',
    inProgress: '进行中',
    completionRate: '完成率',
    successLabel: '成功',
    failedLabel: '失败',
    novaRagEntries: 'nova-RAG 条目',
    novaRagSize: 'nova-RAG 大小',
    memoryFiles: 'Memory 文件',
    memorySize: 'Memory 大小',
    all: '全部',
    unknown: '未知',
    itemUnit: '项',
    noMatches: '无匹配结果',
    lowActivity: '低活跃',
    highActivity: '高活跃',
    timeSlotLabels: ['上午', '下午', '晚上', '凌晨'],
    dayNames: ['周日','周一','周二','周三','周四','周五','周六'],
    noLessons: '暂无教训记录',
    noMatchingLessons: '无匹配的教训记录',
    suggestionLabel: '建议',
    recordLabel: '记录',
    cumulativeTokens: '累计 Tokens',
    comparedWithPrevious: '较上一周期',
    totalTokenMetric: '总 Token',
    totalMessageMetric: '总消息',
    cacheRateMetric: '缓存率',
  },
  en: {
    dayUnit: 'days',
    dayDataUnit: 'days of data',
    weeklyReport: 'Weekly Report',
    monthlySummary: 'Monthly Summary',
    diaryNavDiary: 'Diary',
    diaryNavLoading: 'Click a date in the sidebar to load',
    diaryNavWeeklyOverview: 'Weekly Overview',
    diaryNavMonthlyOverview: 'Monthly Overview',
    detailedDiary: 'Detailed Diary',
    saveChanges: 'Save Changes',
    exportPrint: 'Export',
    exportPrintTitle: 'Export / Print',
    refreshAssets: 'Update Assets',
    refreshAssetsTitle: 'Rebuild asset projection in the background',
    loadingWeekly: 'Reading weekly report snapshot...',
    loadingMonthly: 'Reading monthly report snapshot...',
    loadingDiary: 'Reading diary snapshot...',
    loadingEllipsis: 'Loading...',
    loadFailed: 'Load failed: ',
    weeklySummary: 'Weekly Summary',
    monthlySummarySection: 'Monthly Summary',
    generateMonthlySummaryTitle: 'Generate and save this month summary snapshot',
    periodSummary: 'Period Summary',
    generateSummary: 'Generate Summary',
    generateWeeklySummaryTitle: 'Generate and save this week summary snapshot',
    overview: 'Weekly Overview',
    monthlyOverview: 'Monthly Overview',
    periodOverview: 'Period Overview',
    monthlyPulse: 'Monthly Pulse',
    monthlyPulseScale: 'Scale',
    monthlyPulseRhythm: 'Rhythm',
    monthlyPulseQuality: 'Efficiency',
    monthlyPulseReliability: 'Reliability',
    timeInvestment: 'Time Investment',
    usageTrend: 'Weekly Usage Trend',
    monthlyUsageTrend: 'Monthly Usage Trend',
    periodUsageTrend: 'Period Usage Trend',
    modelRanking: 'Model Usage Ranking',
    agentWorkspaceRanking: 'Agent / Workspace Weekly Ranking',
    monthlyAgentWorkspaceRanking: 'Agent / Workspace Monthly Ranking',
    periodAgentWorkspaceRanking: 'Agent / Workspace Ranking',
    tokenRank: 'Token Usage Ranking',
    messageActivity: 'Message Activity',
    activeDays: 'Active Days',
    ragChange: 'RAG Change',
    summaryDetails: 'Summary Details',
    highFrequencyTopics: 'High-Frequency Topics',
    topicSource: 'Extracted by the report-summary LLM from current-period data; refresh after generating or updating the summary.',
    taskCompletion: 'Task Completion Rate',
    workloadComparison: 'Workload',
    scheduledJobs: 'Scheduled Jobs',
    knowledgeBase: 'Knowledge Base',
    weeklyKnowledgePeriod: 'Weekly',
    monthlyKnowledgePeriod: 'Monthly',
    tasksOutcomes: 'Tasks and Outcomes',
    lessons: 'Lessons and Experience',
    noTopics: 'No LLM high-frequency topics yet; generate a summary snapshot first.',
    snapshotMissingTitle: 'Foundation snapshot is missing for this period',
    snapshotMissingDesc: 'Live recomputation is disabled to avoid long waits. Rebuild data in the background, then this page will refresh automatically.',
    diarySnapshotMissingTitle: 'Foundation snapshot is missing for this day',
    diarySnapshotMissingDesc: 'Markdown fallback is disabled to avoid long waits. Rebuild this day in the background, then this page will refresh automatically.',
    rebuildData: 'Rebuild Data',
    source: 'Source',
    status: 'Status',
    generated: 'Generated',
    noSummaryPrefix: 'No summary snapshot',
    clickGenerateSummary: '; generate a summary.',
    sentenceEnd: '.',
    emptySummary: 'Summary snapshot is empty.',
    highlights: 'Key Outcomes',
    retrospective: 'Retrospective Notes',
    strength: 'Strength',
    mentioned: 'Mentioned',
    submitting: 'Submitting...',
    queued: 'Queued...',
    updating: 'Updating...',
    generating: 'Generating...',
    updateFailed: 'Update failed',
    generationFailed: 'Generation failed',
    assetRefreshFailed: 'Asset projection update failed: ',
    summaryRefreshFailed: 'Summary snapshot generation failed: ',
    foundationSnapshot: 'Foundation snapshot',
    snapshot: 'snapshot',
    snapshotMissing: 'Snapshot missing; update assets',
    foundationPage: 'Foundation page',
    pageSnapshot: 'Page snapshot',
    pageSnapshotMissing: 'Page snapshot missing',
    summarySnapshot: 'Summary snapshot',
    summarySnapshotMissing: 'Summary snapshot missing',
    weekLabel: (reportId) => `${wrDisplayWeekId(reportId)} Weekly Report`,
    monthlyOverviewTitle: 'Monthly Reports',
    monthLabel: (year, month) => `${year}-${String(month).padStart(2, '0')}`,
    cacheHitRate: 'Cache Hit Rate',
    noData: 'No data',
    workspaceUsageNote: 'Uses AI Assets same-session workspace attribution; workspaces below 10M tokens are hidden.',
    agentFallbackUsageNote: 'No attributable workspace data is available, so daily diary agent summaries are shown instead.',
    dayRatio: (value, total) => `${value}/${total} days`,
    currentRagTotal: 'Current nova-RAG Total',
    currentMemoryTotal: 'Current Memory Total',
    noComparableSnapshot: 'No comparable snapshot',
    noComparablePeriod: 'No previous-period snapshot',
    snapshotDelta: (label, from, to) => `${label} snapshot delta (${from} baseline to ${to})`,
    periodDelta: (label) => `${label} delta`,
    countItems: 'items',
    fileItems: 'files',
    activeSessions: 'Active Sessions',
    accumulated: 'Cumulative',
    cronJobsLabel: 'Scheduled Jobs',
    active: 'Active',
    completed: 'Completed',
    inProgress: 'In Progress',
    completionRate: 'Completion Rate',
    successLabel: 'Success',
    failedLabel: 'Failed',
    novaRagEntries: 'nova-RAG Entries',
    novaRagSize: 'nova-RAG Size',
    memoryFiles: 'Memory Files',
    memorySize: 'Memory Size',
    all: 'All',
    unknown: 'Unknown',
    itemUnit: 'items',
    noMatches: 'No matches',
    lowActivity: 'Low activity',
    highActivity: 'High activity',
    timeSlotLabels: ['Morning', 'Afternoon', 'Evening', 'Late night'],
    dayNames: ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'],
    noLessons: 'No lesson records',
    noMatchingLessons: 'No matching lesson records',
    suggestionLabel: 'Suggestion',
    recordLabel: 'Record',
    cumulativeTokens: 'Cumulative Tokens',
    comparedWithPrevious: 'vs previous period',
    totalTokenMetric: 'Total Tokens',
    totalMessageMetric: 'Total Messages',
    cacheRateMetric: 'Cache Rate',
  },
};

const DASHBOARD_SHELL_TEXT = {
  zh: {
    documentTitle: 'Actanara',
    sseConnecting: '⏳ 连接中',
    navOverview: '总览',
    navTodayOverview: '当日实时总览',
    navAiAssets: 'AI 资产',
    navTaskBoardBeta: '任务看板 (Beta) ↗',
    navFoundationOps: 'Foundation 运维',
    settingsButton: '⚙️ 设置',
    settingsTitle: '系统设置',
    llmButton: '🔑 LLM',
    llmTitle: '日记生成 LLM Provider',
    githubTitle: 'GitHub 项目主页待配置',
    i18nTitle: '中英文切换待实现',
    historyBackfill: '生成历史数据',
    backgroundTasksMonitor: '后台任务监控',
    backgroundTasks: '后台任务',
    messagesTitle: '消息与待处理事项',
    messagesShort: '消息',
    overviewTitle: '当日实时总览',
    loadingDots: '加载中...',
    loadingEllipsis: '加载中…',
    realtimeMonitoring: '实时监控',
    realtimeData: '实时数据',
    taskBoard: '任务看板',
    realtimeSync: '实时同步',
    totalTokens: '总消耗 Token',
    totalMessages: '总消息数',
    cacheHitRate: '缓存命中率',
    currentRate: '当前速率',
    activeTools: '活跃工具',
    tokenUnit: 'tokens',
    messageUnitShort: 'msgs',
    token24h: '24 小时 Token 消耗',
    toolDetails: '工具详情',
    agentWorkspaceRealtime: 'Agent / Workspace 实时消耗',
    agentWorkspaceHeader: 'Agent / Workspace',
    noWorkspaceUsageToday: '今日暂无 Agent / Workspace 消耗数据',
    todayTokens: '今日 Tokens',
    currentHour: '当前小时',
    active: '活跃',
    usedToday: '今日使用',
    realtimeUpdatedAt: '更新于 ',
    todayDetails: '今日 · 点击查看详情',
    protocolTotal: '协议总量',
    input: '输入',
    output: '输出',
    waitingRealtimeUsage: '等待实时消耗数据...',
    foundationOpsTitle: 'Foundation 运维',
    foundationOpsSubtitle: 'Daily QA、快照刷新与 job 状态',
    dailyQaSubtitle: '按业务日期检查日记产物、Foundation 输入与 pipeline 恢复状态',
    readQa: '读取 QA',
    dailyPipelineResult: '当天管线结果',
    dailyPipelineSubtitle: '按业务日期查看最新运行、生成文件、lesson 与 task evidence 指标',
    readMetrics: '读取指标',
    repairAudit: '修复执行审计',
    repairAuditSubtitle: '最近的 allowlisted Daily QA repair runs；非 allowlisted 动作仍保持 manual-only',
    reload: '重新读取',
    refreshToday: '刷新今日',
    refreshWeek: '刷新本周',
    refreshMonth: '刷新本月',
    reloadStatus: '重新读取状态',
    historicalBackfill: '历史 Backfill',
    start: '开始',
    end: '结束',
    submitBackfill: '提交 Backfill',
    latestFailed: '最近失败',
    ragSubtitle: 'Actanara v2 长期记忆',
    readStatus: '读取状态…',
    startRagServer: '启动 Server',
    stopRagServer: '停止 Server',
    refreshStatus: '刷新状态',
    migrateRag: '迁移RAG基座/模式',
    registerExternalSkill: '注册外部 Agent Skill',
    coverageCheck: '覆盖检查',
    notReadYet: '尚未读取',
    ragSearchPlaceholder: '搜索 Actanara 长期记忆',
    search: '搜索',
    projectOptional: 'project，可选',
    sourceSetsOptional: 'sourceSets，逗号分隔，可选',
    lifecycleOptional: 'lifecycle，逗号分隔，可选',
    waitingSearch: '等待搜索',
    aiAssetsTitle: 'AI 资产总览',
    aiAssetsSubtitle: '全维度数据概览 · 自动采集',
    refresh: '刷新',
    backgroundUpdate: '后台更新',
    loadingAiAssets: '正在加载 AI 资产数据…',
    toolComparison: '工具消耗对比',
    trend30d: '30 天趋势',
    aiAssetsWorkspaceRanking: 'Agent / Workspace 消耗排行',
    aiAssetsModelTokenRanking: '模型 Token 消耗排行',
    aiAssetsAgentList: 'Agent 列表',
    modelLabel: '模型',
    messages: '消息数',
    lastActive: '最后活跃',
    assetAccumulation: '资产积累',
    diaryStats: '日记统计',
    infrastructure: '基础设施',
    storageUsage: '存储使用',
    taskOverview: '任务总览',
    agentConfigPanel: 'Agent 配置面板',
    skillLibrary: 'Skill 库',
    toolConfig: '工具配置',
    runtimeRegistry: '运行环境登记',
    rediscover: '重新检测',
    tool: '工具',
    runtimeInfo: '运行信息',
    path: '路径',
    detection: '检测',
    back: '返回',
    close: '关闭',
    cancel: '取消',
    save: '保存',
    welcomeTagline: '专属于你的AI资产，是你这个时代最重要的资产',
    modalBack: '← 返回',
  },
  en: {
    documentTitle: 'Actanara',
    sseConnecting: '⏳ Connecting',
    navOverview: 'Overview',
    navTodayOverview: 'Today Live Overview',
    navAiAssets: 'AI Assets',
    navTaskBoardBeta: 'Task Board (Beta) ↗',
    navFoundationOps: 'Foundation Ops',
    settingsButton: '⚙️ Settings',
    settingsTitle: 'System Settings',
    llmButton: '🔑 LLM',
    llmTitle: 'Diary Generation LLM Provider',
    githubTitle: 'GitHub project home not configured',
    i18nTitle: 'Language switching pending',
    historyBackfill: 'Generate Historical Data',
    backgroundTasksMonitor: 'Background Task Monitor',
    backgroundTasks: 'Background Tasks',
    messagesTitle: 'Messages and Action Items',
    messagesShort: 'Messages',
    overviewTitle: 'Today Live Overview',
    loadingDots: 'Loading...',
    loadingEllipsis: 'Loading...',
    realtimeMonitoring: 'Live monitoring',
    realtimeData: 'Live Data',
    taskBoard: 'Task Board',
    realtimeSync: 'Live Sync',
    totalTokens: 'Total Tokens',
    totalMessages: 'Total Messages',
    cacheHitRate: 'Cache Hit Rate',
    currentRate: 'Current Rate',
    activeTools: 'Active Tools',
    tokenUnit: 'tokens',
    messageUnitShort: 'msgs',
    token24h: '24h Token Usage',
    toolDetails: 'Tool Details',
    agentWorkspaceRealtime: 'Agent / Workspace Live Usage',
    agentWorkspaceHeader: 'Agent / Workspace',
    noWorkspaceUsageToday: 'No Agent / Workspace usage data today',
    todayTokens: 'Today Tokens',
    currentHour: 'Current Hour',
    active: 'Active',
    usedToday: 'Used today',
    realtimeUpdatedAt: 'Updated at ',
    todayDetails: 'Today · click for details',
    protocolTotal: 'Protocol total',
    input: 'input',
    output: 'output',
    waitingRealtimeUsage: 'Waiting for live usage data...',
    foundationOpsTitle: 'Foundation Ops',
    foundationOpsSubtitle: 'Daily QA, snapshot refresh, and job status',
    dailyQaSubtitle: 'Check diary artifacts, Foundation inputs, and pipeline recovery status by business date',
    readQa: 'Read QA',
    dailyPipelineResult: 'Daily Pipeline Result',
    dailyPipelineSubtitle: 'View the latest run, generated files, lessons, and task evidence metrics by business date',
    readMetrics: 'Read Metrics',
    repairAudit: 'Repair Execution Audit',
    repairAuditSubtitle: 'Recent allowlisted Daily QA repair runs; non-allowlisted actions remain manual-only',
    reload: 'Reload',
    refreshToday: 'Refresh Today',
    refreshWeek: 'Refresh This Week',
    refreshMonth: 'Refresh This Month',
    reloadStatus: 'Reload Status',
    historicalBackfill: 'Historical Backfill',
    start: 'Start',
    end: 'End',
    submitBackfill: 'Submit Backfill',
    latestFailed: 'Latest Failed',
    ragSubtitle: 'Actanara v2 long-term memory',
    readStatus: 'Read Status...',
    startRagServer: 'Start Server',
    stopRagServer: 'Stop Server',
    refreshStatus: 'Refresh Status',
    migrateRag: 'Migrate RAG Profile / Mode',
    registerExternalSkill: 'Register External Agent Skill',
    coverageCheck: 'Coverage Check',
    notReadYet: 'Not read yet',
    ragSearchPlaceholder: 'Search Actanara long-term memory',
    search: 'Search',
    projectOptional: 'project, optional',
    sourceSetsOptional: 'sourceSets, comma-separated, optional',
    lifecycleOptional: 'lifecycle, comma-separated, optional',
    waitingSearch: 'Waiting for search',
    aiAssetsTitle: 'AI Assets Overview',
    aiAssetsSubtitle: 'Full-dimensional data overview · automatic collection',
    refresh: 'Refresh',
    backgroundUpdate: 'Background Update',
    loadingAiAssets: 'Loading AI Assets data...',
    toolComparison: 'Tool Usage Comparison',
    trend30d: '30-Day Trend',
    aiAssetsWorkspaceRanking: 'Agent / Workspace Usage Ranking',
    aiAssetsModelTokenRanking: 'Model Token Usage Ranking',
    aiAssetsAgentList: 'Agent List',
    modelLabel: 'Model',
    messages: 'Messages',
    lastActive: 'Last Active',
    assetAccumulation: 'Asset Accumulation',
    diaryStats: 'Diary Stats',
    infrastructure: 'Infrastructure',
    storageUsage: 'Storage Usage',
    taskOverview: 'Task Overview',
    agentConfigPanel: 'Agent Configuration Panel',
    skillLibrary: 'Skill Library',
    toolConfig: 'Tool Configuration',
    runtimeRegistry: 'Runtime Registry',
    rediscover: 'Rediscover',
    tool: 'Tool',
    runtimeInfo: 'Runtime Info',
    path: 'Path',
    detection: 'Detection',
    back: 'Back',
    close: 'Close',
    cancel: 'Cancel',
    save: 'Save',
    welcomeTagline: 'Your AI assets are the most important assets of this era.',
    modalBack: '← Back',
  },
};

// Static contract anchors for layout-order tests:
// ⏱️</span> 时间投入 precedes ⚡</span> 周度使用趋势 in the weekly report layout.

const FOUNDATION_TEXT = {
  zh: {
    statusLabels: {
      ready: '就绪', complete: '完成', completed: '完成', attention: '需关注', warning: '需关注',
      blocked: '阻塞', failed: '失败', queued: '排队中', running: '运行中', incomplete: '未完整',
      missing: '缺失', neutral: '无变化', unknown: '未知',
      scheduled: '已预约', starting: '启动中', stopping: '停止中', cancel_requested: '取消中',
      partial: '部分完成', cancelled: '已取消', interrupted: '已中断', configured: '已配置', stopped: '已停止',
    },
    backgroundTasks: '后台任务监控',
    status: '状态',
    activeBackgroundTasks: (count) => `有 ${count} 个后台任务正在运行`,
    dateRangeTo: ' 至 ',
    documents: { narrative: '叙事日记', technical: '技术进展', learning: '智慧沉淀' },
    complete: '完整',
    missing: '缺失',
    impactPrefix: '影响：',
    actionPrefix: '建议：',
    repairCommands: '修复命令',
    copied: '已复制',
    copyFailed: '复制失败',
    copyCommand: '复制命令',
    manualOnly: '执行状态：manual-only',
    dashboardExecutable: 'Dashboard 可执行',
    copyCommandOnly: '仅复制命令',
    requiresLock: '需要锁',
    requiresConfirmation: '需要确认：',
    requiresAudit: '需要审计',
    execute: '执行',
    submitting: '提交中…',
    queued: '排队中…',
    running: '执行中…',
    executionFailed: '执行失败: ',
    backgroundUpdateFailed: '后台更新失败',
    repairStatusReadFailed: '修复状态读取失败: HTTP ',
    repairStillRunning: '修复仍在后台运行，请稍后重新加载',
    repairRunFailed: '修复执行失败',
    dailyQaReadFailed: 'Daily QA 读取失败: ',
    businessDate: '业务日期',
    documentsStat: '文档',
    blockers: 'Blockers',
    warnings: 'Warnings',
    latestFoundationRun: '最新 Foundation Run',
    generatedAt: '生成时间',
    foundationInputs: 'Foundation 输入',
    generatedArtifacts: '生成产物',
    recommendedActions: '建议动作',
    pipelineReadFailed: '当天管线指标读取失败: ',
    runStatus: '运行状态',
    activity: 'Activity',
    activeState: 'Active',
    noActivity: 'No activity',
    latestRun: '最新 Run',
    duration: '耗时',
    fileSize: '文件大小',
    taskCandidates: 'Task candidates',
    taskUpdates: 'Task updates',
    lessons: 'Lessons',
    materialization: 'Materialization',
    blankInputs: 'Blank inputs',
    noGeneratedFileProjection: '暂无生成文件投影',
    blankTaskSkipped: 'Blank-day fast path 已跳过 Nova-Task evidence',
    noTaskEvidence: '暂无 Nova-Task evidence',
    noLesson: '暂无 lesson',
    latestFailedStep: '最近失败步骤：',
    blankDayFastPath: 'Blank-day fast path',
    blankDayDesc: '当日无活动，已跳过 Narrative / Technical / Learning / Nova-Task evidence，仅写入 no-activity 日记投影。',
    start: '开始',
    finish: '完成',
    type: 'Type',
    file: 'File',
    size: 'Size',
    sections: 'Sections',
    repairAuditReadFailed: '修复执行审计读取失败: ',
    noRepairRuns: '暂无 Dashboard repair run 审计记录',
    updated: 'Updated',
    error: 'Error',
    recentQaReadFailed: '最近 7 天 QA 读取失败: ',
    recentDays: (days) => `最近 ${days} 天`,
    ready: 'Ready',
    attention: 'Attention',
    docs: 'docs',
    inputs: 'inputs',
    readingDailyQa: '读取 Daily QA…',
    readingPipeline: '读取当天管线指标…',
    readingRepairAudit: '读取修复执行审计…',
    readingRecentQa: '读取最近 7 天 QA…',
    noCompletenessData: '无 completeness 数据',
    required: 'Required',
    total: 'Total',
    projection: 'Projection',
    sourceRun: 'Source Run',
    detail: 'Detail',
    run: 'Run',
    action: 'Action',
    date: 'Date',
    scope: 'Scope',
    period: 'Period',
    statusHeader: 'Status',
    started: 'Started',
    completedAt: 'Completed',
    unknownFailure: 'unknown failure',
    dashboardScheduler: 'Dashboard scheduler',
    schedulerLoop: 'Scheduler loop',
    systemTimer: 'System timer',
    nextAggregation: 'Next aggregation',
    dailyPipeline: 'Daily pipeline',
    dashboardAggregation: 'Dashboard aggregation',
    latestJob: 'Latest job',
    lastError: 'Last error',
    registered: 'registered',
    unsupported: 'unsupported',
    notRegistered: 'not registered',
    database: 'Database',
    dbExists: 'DB exists',
    yes: 'yes',
    no: 'no',
  },
  en: {
    statusLabels: {
      ready: 'Ready', complete: 'Complete', completed: 'Completed', attention: 'Attention', warning: 'Attention',
      blocked: 'Blocked', failed: 'Failed', queued: 'Queued', running: 'Running', incomplete: 'Incomplete',
      missing: 'Missing', neutral: 'No change', unknown: 'Unknown',
      scheduled: 'Scheduled', starting: 'Starting', stopping: 'Stopping', cancel_requested: 'Cancelling',
      partial: 'Partial', cancelled: 'Cancelled', interrupted: 'Interrupted', configured: 'Configured', stopped: 'Stopped',
    },
    backgroundTasks: 'Background task monitor',
    status: 'Status',
    activeBackgroundTasks: (count) => `${count} background task${Number(count) === 1 ? '' : 's'} running`,
    dateRangeTo: ' to ',
    documents: { narrative: 'Narrative Diary', technical: 'Technical Report', learning: 'Learning Audit' },
    complete: 'Complete',
    missing: 'Missing',
    impactPrefix: 'Impact: ',
    actionPrefix: 'Action: ',
    repairCommands: 'Repair Commands',
    copied: 'Copied',
    copyFailed: 'Copy failed',
    copyCommand: 'Copy Command',
    manualOnly: 'Execution state: manual-only',
    dashboardExecutable: 'Dashboard executable',
    copyCommandOnly: 'Copy command only',
    requiresLock: 'requires lock',
    requiresConfirmation: 'requires confirmation: ',
    requiresAudit: 'requires audit',
    execute: 'Run',
    submitting: 'Submitting...',
    queued: 'Queued...',
    running: 'Running...',
    executionFailed: 'Execution failed: ',
    backgroundUpdateFailed: 'Background update failed',
    repairStatusReadFailed: 'Repair status read failed: HTTP ',
    repairStillRunning: 'Repair is still running in the background; reload later.',
    repairRunFailed: 'Repair run failed',
    dailyQaReadFailed: 'Daily QA read failed: ',
    businessDate: 'Business Date',
    documentsStat: 'Documents',
    blockers: 'Blockers',
    warnings: 'Warnings',
    latestFoundationRun: 'Latest Foundation Run',
    generatedAt: 'Generated At',
    foundationInputs: 'Foundation Inputs',
    generatedArtifacts: 'Generated Artifacts',
    recommendedActions: 'Recommended Actions',
    pipelineReadFailed: 'Daily pipeline metrics read failed: ',
    runStatus: 'Run Status',
    activity: 'Activity',
    activeState: 'Active',
    noActivity: 'No activity',
    latestRun: 'Latest Run',
    duration: 'Duration',
    fileSize: 'File Size',
    taskCandidates: 'Task Candidates',
    taskUpdates: 'Task Updates',
    lessons: 'Lessons',
    materialization: 'Materialization',
    blankInputs: 'Blank Inputs',
    noGeneratedFileProjection: 'No generated file projection',
    blankTaskSkipped: 'Blank-day fast path skipped Nova-Task evidence',
    noTaskEvidence: 'No Nova-Task evidence',
    noLesson: 'No lessons',
    latestFailedStep: 'Latest failed step: ',
    blankDayFastPath: 'Blank-day fast path',
    blankDayDesc: 'No activity for this day; Narrative / Technical / Learning / Nova-Task evidence were skipped and only the no-activity diary projection was written.',
    start: 'Started',
    finish: 'Completed',
    type: 'Type',
    file: 'File',
    size: 'Size',
    sections: 'Sections',
    repairAuditReadFailed: 'Repair execution audit read failed: ',
    noRepairRuns: 'No Dashboard repair run audit records',
    updated: 'Updated',
    error: 'Error',
    recentQaReadFailed: 'Recent 7-day QA read failed: ',
    recentDays: (days) => `Last ${days} days`,
    ready: 'Ready',
    attention: 'Attention',
    docs: 'docs',
    inputs: 'inputs',
    readingDailyQa: 'Reading Daily QA...',
    readingPipeline: 'Reading daily pipeline metrics...',
    readingRepairAudit: 'Reading repair execution audit...',
    readingRecentQa: 'Reading recent 7-day QA...',
    noCompletenessData: 'No completeness data',
    required: 'Required',
    total: 'Total',
    projection: 'Projection',
    sourceRun: 'Source Run',
    detail: 'Detail',
    run: 'Run',
    action: 'Action',
    date: 'Date',
    scope: 'Scope',
    period: 'Period',
    statusHeader: 'Status',
    started: 'Started',
    completedAt: 'Completed',
    unknownFailure: 'unknown failure',
    dashboardScheduler: 'Dashboard scheduler',
    schedulerLoop: 'Scheduler loop',
    systemTimer: 'System timer',
    nextAggregation: 'Next aggregation',
    dailyPipeline: 'Daily pipeline',
    dashboardAggregation: 'Dashboard aggregation',
    latestJob: 'Latest job',
    lastError: 'Last error',
    registered: 'registered',
    unsupported: 'unsupported',
    notRegistered: 'not registered',
    database: 'Database',
    dbExists: 'DB exists',
    yes: 'yes',
    no: 'no',
  },
};

const RAG_UI_TEXT = {
  zh: {
    powerDisable: '停用 nova-RAG',
    powerEnable: '启用 nova-RAG',
    powerInit: '初始化 nova-RAG',
    powerDisableTitle: '停止 nova-RAG search server 并关闭产品开关',
    powerEnableTitle: '启用 nova-RAG 并启动 search server',
    powerInitTitle: '选择参数并创建 nova-RAG 基座',
    instantParams: '即时参数',
    configured: 'Configured',
    activeIndex: 'Active index',
    noActiveProfile: 'no active profile',
    policy: 'local/cloud/model/dimension 由基座 profile 锁定；变更请走“迁移RAG基座/模式”。语言跟随 Actanara 全局 locale。',
    timeHalfLife: '时间半衰期',
    saveInstantParams: '保存即时参数',
    refreshStatus: '刷新 nova-RAG 状态…',
    refreshFailed: '刷新失败: ',
    enabling: '启用中…',
    disabling: '停用中…',
    enablingServer: '启用 nova-RAG 并启动 search server…',
    disablingServer: '停止 nova-RAG 服务并关闭产品开关…',
    startingServer: '启动 nova-RAG search server…',
    stoppingServer: '停止 nova-RAG search server…',
    serverStartRequested: 'nova-RAG 服务已请求启动',
    serverStopRequested: 'nova-RAG 服务已请求停止',
    disabledDone: 'nova-RAG 已停用',
    startFailed: '启动失败: ',
    stopFailed: '停止失败: ',
    savingSettings: '保存 nova-RAG 设置…',
    savedSettings: '已保存 nova-RAG 设置 ',
    saveFailed: '保存失败: ',
    disabledNeedEnable: 'nova-RAG 总开关已关闭；请先启用 nova-RAG 并保存设置。',
    confirmationPrompt: '输入确认短语以执行 nova-RAG search server 操作：',
    confirmationMismatch: '操作已取消：确认短语不匹配。',
    executing: (action) => `执行 ${action}…`,
    queuedJob: '已加入后台任务 ',
    actionFailed: '执行失败: ',
    loadingCoverage: '读取 nova-RAG 覆盖…',
    loadingCoverageInfo: '读取覆盖信息…',
    coverage: '覆盖',
    path: '路径',
    missing: '缺失: ',
    allCovered: '配置 sourceSets 均已覆盖。',
    coverageFailed: '覆盖检查失败: ',
    coverageReadFailed: '覆盖读取失败: ',
    dateCoverage: '日期覆盖',
    missingRagIndexDates: '只缺 RAG index 的日期: ',
    missingUpstreamDates: '上游缺失日期: ',
    ragIndexDateRanges: 'RAG index 待同步范围: ',
    upstreamDateRanges: '上游待补齐范围: ',
    recommendRagSync: '建议运行生产 RAG Sync。',
    recommendUpstreamBackfill: '建议补跑 daily pipeline / Foundation materialization。',
    runProductionRagSync: '运行生产 RAG Sync',
    openFoundationBackfill: '打开历史数据回填',
    openFoundationOps: '打开 Foundation 运维',
    closePanel: '关闭',
    productionSync: '生产 RAG Sync',
    productionSyncConfirmationPrompt: '输入确认短语以提交生产 RAG Sync，该操作会构建候选索引并在通过 gate 后提升 active index：',
    productionSyncConfirmationMismatch: '生产 RAG Sync 已取消：确认短语不匹配。',
    productionSyncQueued: '生产 RAG sync 已加入后台任务：',
    productionSyncFailed: '生产 RAG sync 提交失败: ',
    runningEval: '运行 nova-RAG eval…',
    retrievalEval: '检索评估',
    evalFailed: '评估失败: ',
    searching: '搜索中…',
    noResults: '无结果',
    searchFailed: '搜索失败: ',
    searchQueryRequired: '请输入搜索内容。',
    searchUnavailable: '检索不可用: ',
    readingStatus: '读取 nova-RAG 状态…',
    readingSettings: '读取 nova-RAG 设置…',
    statusReadFailed: '状态读取失败: ',
    settingsReadFailed: '设置读取失败: ',
    status: '状态',
    mode: '模式',
    product: '产品',
    search: '检索',
    server: 'Server',
    ready: 'Ready',
    configuredProfile: 'Configured Profile',
    activeProfile: 'Active Profile',
    activeRun: 'Active Run',
    chunks: 'Chunks',
    documents: 'Documents',
    enabled: 'enabled',
    disabled: 'disabled',
    available: 'available',
    unavailable: 'unavailable',
    healthy: 'healthy',
    offline: 'offline',
    none: 'none',
    globalDisabled: 'nova-RAG 总开关已关闭：',
    migrationRequired: 'Migration required: configured profile 与 active index profile 不一致。',
    searchUnavailableHint: '检索需要 nova-RAG search server 可用；基座/模式变更请先执行迁移并 promote candidate。',
    migrationInitTitle: '初始化 nova-RAG',
    migrationTitle: '迁移RAG基座/模式',
    migrationInitAction: '确认初始化并加入后台任务',
    migrationAction: '提交后台迁移',
    migrationInitNote: '初始化会保存 nova-RAG 配置，在后台生成 v2 candidate index，并在成功后自动 promote 为 active，使检索进入可用状态。local 模式可能首次下载或加载 embedding 模型。',
    migrationNote: '迁移会在后台生成新的 v2 candidate index。active index 不会被覆盖；完成后仍需要单独 compare/eval/promote。local 模式可能拉取或加载 embedding 模型，cloud 模式可能产生 API 成本。',
    migrationPreviewAction: '预览执行计划',
    migrationPlanPrefix: '计划: ',
    migrationPlanFailed: '计划读取失败: ',
    initializationConfirmation: 'INITIALIZE ACTANARA RAG',
    initializationConfirmationMismatch: '初始化已取消：确认短语不匹配。',
    targetProfile: '目标 Profile',
    sourceRootOverride: '覆盖源路径',
    cloudEndpointPlaceholder: '仅 cloud 模式需要',
    confirmationPhrase: '确认短语',
    viewBackgroundTasks: '查看后台任务',
    submittingInitTask: '提交初始化任务…',
    submittingMigrationTask: '提交迁移任务…',
    initSubmitting: '初始化提交中…',
    migrationSubmitting: '迁移提交中…',
    queuedTaskPrefix: '已加入后台任务：',
    initSubmitFailed: '初始化提交失败: ',
    migrationSubmitFailed: '迁移提交失败: ',
    confirmMigrationFallback: '确认迁移并加入后台任务',
    externalSkillTitle: '注册外部 Agent Memory Skill',
    externalSkillNote: '将 nova-RAG 作为 read-only global skill 注册到 OpenClaw、Claude Code、Codex、Gemini CLI、Hermes。默认 dry-run；实际安装需要确认短语。',
    externalSkillPolicy: 'read-only；不允许 memory write、index run、server lifecycle mutation。',
    overwriteSkill: '覆盖已有 skill（会先备份）',
    refreshPlan: '刷新计划',
    installSkill: '安装 Skill',
    readContract: '读取 Contract',
    backgroundTasks: '后台任务',
    readingInstallPlan: '读取安装计划…',
    noRegistrationTargets: '没有可注册目标',
    planReadFailed: '读取计划失败: ',
    submittingRegistration: '提交注册任务…',
    registrationComplete: '注册完成：',
    registrationFailed: '注册失败: ',
    readingContract: '读取 contract…',
    readFailed: '读取失败: ',
  },
  en: {
    powerDisable: 'Disable nova-RAG',
    powerEnable: 'Enable nova-RAG',
    powerInit: 'Initialize nova-RAG',
    powerDisableTitle: 'Stop the nova-RAG search server and disable the product switch',
    powerEnableTitle: 'Enable nova-RAG and start the search server',
    powerInitTitle: 'Choose parameters and create the nova-RAG base profile',
    instantParams: 'Runtime Parameters',
    configured: 'Configured',
    activeIndex: 'Active Index',
    noActiveProfile: 'no active profile',
    policy: 'The base profile locks local/cloud/model/dimension. Use “Migrate RAG profile/mode” for changes. Language follows the global Actanara locale.',
    timeHalfLife: 'Recency Half-Life',
    saveInstantParams: 'Save Runtime Parameters',
    refreshStatus: 'Refreshing nova-RAG status...',
    refreshFailed: 'Refresh failed: ',
    enabling: 'Enabling...',
    disabling: 'Disabling...',
    enablingServer: 'Enabling nova-RAG and starting the search server...',
    disablingServer: 'Stopping nova-RAG services and disabling the product switch...',
    startingServer: 'Starting nova-RAG search server...',
    stoppingServer: 'Stopping nova-RAG search server...',
    serverStartRequested: 'nova-RAG service start requested',
    serverStopRequested: 'nova-RAG service stop requested',
    disabledDone: 'nova-RAG disabled',
    startFailed: 'Start failed: ',
    stopFailed: 'Stop failed: ',
    savingSettings: 'Saving nova-RAG settings...',
    savedSettings: 'Saved nova-RAG settings ',
    saveFailed: 'Save failed: ',
    disabledNeedEnable: 'nova-RAG is disabled; enable nova-RAG and save settings first.',
    confirmationPrompt: 'Enter the confirmation phrase to run the nova-RAG search server action: ',
    confirmationMismatch: 'Action cancelled: confirmation phrase did not match.',
    executing: (action) => `Running ${action}...`,
    queuedJob: 'queued background task ',
    actionFailed: 'Action failed: ',
    loadingCoverage: 'Reading nova-RAG coverage...',
    loadingCoverageInfo: 'Reading coverage information...',
    coverage: 'Coverage',
    path: 'Path',
    missing: 'Missing: ',
    allCovered: 'All configured sourceSets are covered.',
    coverageFailed: 'Coverage check failed: ',
    coverageReadFailed: 'Coverage read failed: ',
    dateCoverage: 'Date Coverage',
    missingRagIndexDates: 'Dates missing only RAG index: ',
    missingUpstreamDates: 'Dates with upstream gaps: ',
    ragIndexDateRanges: 'RAG index sync ranges: ',
    upstreamDateRanges: 'Upstream backfill ranges: ',
    recommendRagSync: 'Run production RAG Sync.',
    recommendUpstreamBackfill: 'Run daily pipeline / Foundation materialization.',
    runProductionRagSync: 'Run Production RAG Sync',
    openFoundationBackfill: 'Open Historical Backfill',
    openFoundationOps: 'Open Foundation Ops',
    closePanel: 'Close',
    productionSync: 'Production RAG Sync',
    productionSyncConfirmationPrompt: 'Enter the confirmation phrase to submit Production RAG Sync. It builds a candidate index and promotes the active index after gates pass: ',
    productionSyncConfirmationMismatch: 'Production RAG Sync cancelled: confirmation phrase did not match.',
    productionSyncQueued: 'production RAG sync queued: ',
    productionSyncFailed: 'Production RAG sync submit failed: ',
    runningEval: 'Running nova-RAG eval...',
    retrievalEval: 'Retrieval Eval',
    evalFailed: 'Eval failed: ',
    searching: 'Searching...',
    noResults: 'No results',
    searchFailed: 'Search failed: ',
    searchQueryRequired: 'Enter a search query.',
    searchUnavailable: 'Search unavailable: ',
    readingStatus: 'Reading nova-RAG status...',
    readingSettings: 'Reading nova-RAG settings...',
    statusReadFailed: 'Status read failed: ',
    settingsReadFailed: 'Settings read failed: ',
    status: 'Status',
    mode: 'Mode',
    product: 'Product',
    search: 'Search',
    server: 'Server',
    ready: 'Ready',
    configuredProfile: 'Configured Profile',
    activeProfile: 'Active Profile',
    activeRun: 'Active Run',
    chunks: 'Chunks',
    documents: 'Documents',
    enabled: 'enabled',
    disabled: 'disabled',
    available: 'available',
    unavailable: 'unavailable',
    healthy: 'healthy',
    offline: 'offline',
    none: 'none',
    globalDisabled: 'nova-RAG is disabled: ',
    migrationRequired: 'Migration required: configured profile does not match the active index profile.',
    searchUnavailableHint: 'Search requires the nova-RAG search server. Run migration and promote a candidate before changing the base profile or mode.',
    migrationInitTitle: 'Initialize nova-RAG',
    migrationTitle: 'Migrate RAG Profile / Mode',
    migrationInitAction: 'Confirm initialization and queue background task',
    migrationAction: 'Submit Background Migration',
    migrationInitNote: 'Initialization saves nova-RAG settings, builds a v2 candidate index in the background, and promotes it to active after success so search becomes available. Local mode may download or load the embedding model on first use.',
    migrationNote: 'Migration builds a new v2 candidate index in the background. The active index is not overwritten; compare/eval/promote still run separately afterward. Local mode may pull or load an embedding model; cloud mode may incur API cost.',
    migrationPreviewAction: 'Preview Plan',
    migrationPlanPrefix: 'Plan: ',
    migrationPlanFailed: 'Plan read failed: ',
    initializationConfirmation: 'INITIALIZE ACTANARA RAG',
    initializationConfirmationMismatch: 'Initialization cancelled: confirmation phrase did not match.',
    targetProfile: 'Target Profile',
    sourceRootOverride: 'Source Root Override',
    cloudEndpointPlaceholder: 'Required only for cloud mode',
    confirmationPhrase: 'Confirmation Phrase',
    viewBackgroundTasks: 'View Background Tasks',
    submittingInitTask: 'Submitting initialization task...',
    submittingMigrationTask: 'Submitting migration task...',
    initSubmitting: 'Submitting initialization...',
    migrationSubmitting: 'Submitting migration...',
    queuedTaskPrefix: 'Queued background task: ',
    initSubmitFailed: 'Initialization submit failed: ',
    migrationSubmitFailed: 'Migration submit failed: ',
    confirmMigrationFallback: 'Confirm migration and queue background task',
    externalSkillTitle: 'Register External Agent Memory Skill',
    externalSkillNote: 'Register nova-RAG as a read-only global skill for OpenClaw, Claude Code, Codex, Gemini CLI, and Hermes. Defaults to dry-run; actual installation requires a confirmation phrase.',
    externalSkillPolicy: 'read-only; memory write, index runs, and server lifecycle mutations are not allowed.',
    overwriteSkill: 'Overwrite existing skill (backs up first)',
    refreshPlan: 'Refresh Plan',
    installSkill: 'Install Skill',
    readContract: 'Read Contract',
    backgroundTasks: 'Background Tasks',
    readingInstallPlan: 'Reading install plan...',
    noRegistrationTargets: 'No registration targets',
    planReadFailed: 'Plan read failed: ',
    submittingRegistration: 'Submitting registration task...',
    registrationComplete: 'Registration complete: ',
    registrationFailed: 'Registration failed: ',
    readingContract: 'Reading contract...',
    readFailed: 'Read failed: ',
  },
};

const OPERATOR_UI_TEXT = {
  zh: {
    action: '操作',
    started: '开始 ',
    completed: '完成 ',
    backgroundTask: '后台任务',
    backgroundTaskActionFailed: '后台任务操作失败: ',
    running: '运行中',
    recentTasks: '最近任务',
    sources: '来源',
    services: '服务',
    statusBreakdown: '状态分布',
    sourceBreakdown: '来源分布',
    backgroundTaskSourceCount: (count) => `已读取 ${count} 类后台来源`,
    noBackgroundTasks: '暂无后台任务记录',
    backgroundTasksReadFailed: '读取后台任务失败: ',
    backgroundTasksTitle: '后台任务监控',
    readingBackgroundTasks: '读取后台任务…',
    choosePeriodFirst: '请先选择周期',
    chooseScheduleTime: '请选择预约生成时间',
    noMissingItems: '当前计划没有待补项。',
    previewFirstItems: '仅预览前 120 个待生成项，其余会继续按后台任务处理。',
    pendingItems: '待生成项',
    missingDiaries: '涉及日期',
    existingDiaries: '已有数据日期',
    missingSummaries: '周/月总结',
    maxLlmCalls: '预计最多 LLM 调用',
    type: '类型',
    pendingItem: '待生成项',
    diary: '日记',
    monthlyReport: '月报',
    weeklyReport: '周报',
    other: '其他',
    calculatingPlan: '计算计划中…',
    dryRunReady: '计划预览已生成',
    planFailed: '计划失败: ',
    dryRunRequiredBeforeQueue: '请先为当前选择生成计划预览，再排队执行。',
    dryRunStale: '当前选择已变化，请重新生成计划预览后再提交。',
    submittingBackgroundTask: '提交后台任务中…',
    checkingExistingDiaries: '检查已有日记数据中…',
    overwriteConfirm: (items) => `这将覆盖已有数据：${items}，是否确认操作？`,
    overwriteCancelled: '已取消覆盖式重新生成',
    scheduled: '已预约',
    startedRun: '已开始',
    runDetailsHint: '，请在后台任务查看详细进度。',
    period: '周期',
    dailyPipeline: '每日管线',
    days: '天',
    overwriteConfirmed: '已确认覆盖式重新生成。',
    historyQueued: 'Historical 回填已进入后台任务队列。该任务同一时间只允许存在一个运行中或预约中的实例。',
    viewBackgroundTasks: '查看后台任务',
    submitFailed: '提交失败: ',
    historyBackfillTitle: '生成历史数据',
    historyBackfillSection: '历史数据回填',
    historyBackfillNote: '适用于新用户已有大量历史数据的场景。任务会按每日完整性契约补齐日记、SQLite、RAG 与 Nova-Task 缺失项，再生成周/月聚合；勾选周/月总结会覆盖当前已有周/月总结并调用当前 LLM Provider。',
    selectedPeriods: '已选择周期',
    choosePeriod: '选择周期',
    runMode: '执行方式',
    runNow: '立即生成',
    runScheduled: '预约生成',
    scheduledTime: '预约时间',
    generateSummaries: '生成周/月总结（会覆盖当前已有周/月总结）',
    skipReady: '跳过已有数据的日期（全量重建）',
    overwriteNote: '取消该选项时，会覆盖式重建所选周期内所有日期；勾选周/月总结时，当前已有周/月总结也会被覆盖。',
    estimateFirst: '请先生成计划预览',
    dryRunEstimate: '计划预览',
    queueGeneration: '排队生成',
    noSelectedPeriods: '尚未选择周期',
    periodPickerTitle: '选择周期',
    periodPickerNote: '选择近半年的月份或周。历史补全会展开到对应日期，优先补齐缺失日记和每日管线数据，再生成周期聚合。',
    cancel: '取消',
    confirmSelection: '确认选择',
    attentionItems: (count) => `有 ${count} 条事项需要处理`,
    messagesTitle: '消息与待处理事项',
    severityError: '失败',
    severityWarn: '待处理',
    severityInfo: '信息',
    read: '已读',
    message: '消息',
    openFailed: '打开失败：',
    actionSubmitted: '操作已提交',
    actionFailed: '操作失败: ',
    noMessages: '暂无需要处理的消息',
    readingMessages: '读取消息…',
    onboardingTitle: '新用户 Readiness',
    onboardingNote: '只读面板。用于查看最小部署、可选子系统依赖、runtime、nova-RAG、scheduler 与资源画像；不会安装、写配置或注册系统任务。',
    refreshOnboarding: '刷新 Onboarding 状态',
    notReadYet: '尚未读取',
    readingOnboardingReadiness: '读取 onboarding readiness…',
    readingOnboardingPlan: '读取 onboarding plan…',
    onboardingReadFailed: 'Onboarding 读取失败: ',
    noSettingsAuthority: '暂无 settings authority inventory。',
    settingsAuthorityTitle: '设置权威 Inventory',
    settingsAuthorityNote: '只读视图。用于识别 settings.json、env override、derived/default 和 manual/auto 状态，不会写入配置。',
    persistentWrite: '持久写入',
    envSemantics: 'Env 语义',
    defaultManual: '默认/手动',
    secret: 'Secret',
    writableVia: '写入入口：',
    productLocalization: '产品与本地化',
    dashboardService: 'Dashboard 服务',
    dashboardRestartNote: '这些值保存到 settings.json；已运行的 Dashboard LaunchAgent 需要重启后才会采用 host/port/python/appDir 等启动参数。',
    restartCommand: '重启命令：',
    copyCommand: '复制命令',
    systemSchedulerMode: '系统 scheduler 模式',
    enableSystemScheduler: '启用系统 scheduler',
    timezone: '时区',
    dailyPipelineTime: '每日管线时间',
    dashboardAggregationTime: 'Dashboard 聚合时间',
    systemTimerProvider: '系统定时器 Provider',
    systemTimerLabel: '系统定时器 Label',
    systemTimerNote: '系统定时任务不会在保存设置时自动写入。安装会创建两个用户级 launchd job：每日管线和 Dashboard Foundation 聚合，并保留备份/卸载路径。',
    previewSystemTimer: '预览系统定时任务',
    installUpdate: '安装/更新',
    uninstall: '卸载',
    autoRefreshTargets: '自动刷新目标',
    currentDaySnapshot: '当天 snapshot',
    currentWeekSnapshot: '本周 snapshot',
    currentMonthSnapshot: '本月 snapshot',
    externalAgentMode: '外部 agent 模式',
    enableExternalAgentMode: '启用外部 agent 模式',
    externalAgentNote: '发送给外部 Agent 后，由外部 Agent 管理系统定时任务；Dashboard 不接管完整生产管线执行。',
    prompt: '提示词',
    currentSelection: '当前选择',
    editedValue: '编辑值',
    readingRuntimePath: '读取当前 runtime path…',
    runtimePathReadFailed: '读取当前路径失败: ',
    processing: '处理中…',
    pathOperationFailed: '路径操作失败: ',
    runtimePathConfirmationPrompt: '输入确认短语以切换或初始化 Actanara runtime path：',
    readingPath: '读取路径…',
    pathReadFailed: '读取路径失败: ',
    parentDirectory: '上级目录',
    chooseCurrentDirectory: '选择当前目录',
    browse: '浏览',
    userPaths: '用户路径',
    userPathsNote: '这些路径会影响新生成产物或后续读取位置。Diary 路径保存后会同步运行时 manifest；已有 sqlite 内容不会自动搬迁文件。',
    snapshotsPath: 'Snapshots 路径',
    diaryPath: 'Diary 路径',
    reportsPath: 'Reports 路径',
    archivesPath: 'Archives 路径',
    taskIntelligencePath: 'Task Intelligence 路径',
    taskBoardPath: 'Task Board 路径',
    ragIndexPath: 'nova-RAG index 路径',
    runtimeHomeNote: '验证/切换 runtime home 会更新 Actanara 当前选择；import legacy 会从 legacy diary root 尝试复制可迁移资产。',
    refreshRuntimePath: '刷新 Runtime Path',
    validateRuntimePath: '验证 Runtime Path',
    useRuntimePath: '使用该 Runtime Path',
    initializeRuntimePath: '初始化 Runtime Path',
    checkDiarySqlite: '检查 Diary / SQLite 一致性',
    featureSettingsNote: '这里只保留产品级开关。LLM 生成、任务审计、Embedding Server 属于默认运行或子系统内部配置，不在普通功能开关中暴露。',
    featureDailyPipeline: '每日管线',
    featureDashboard: 'Dashboard 服务',
    saved: '已保存 ',
    restartRequiredSaved: '；启动参数变更需重启 Dashboard 后完全应用。命令：',
    saveFailed: '保存失败: ',
    readingSystemTimerPreview: '读取系统定时任务预览…',
    previewFailed: '预览失败: ',
    installTimerPrompt: '输入确认短语以安装系统定时任务：',
    uninstallTimerPrompt: '输入确认短语以卸载系统定时任务：',
    installCancelledMismatch: '安装已取消：确认短语不匹配。',
    uninstallCancelledMismatch: '卸载已取消：确认短语不匹配。',
    installingSystemTimer: '安装系统定时任务…',
    uninstallingSystemTimer: '卸载系统定时任务…',
    installedJobs: (count) => `已安装 ${count} 个 launchd job。Backup: `,
    uninstalledJobs: (count) => `已卸载 ${count} 个 launchd job。Backup: `,
    installFailed: '安装失败: ',
    uninstallFailed: '卸载失败: ',
    githubProject: 'GitHub 项目主页',
    githubTodo: 'GitHub 跳转链接待配置。该按钮已预留，确认项目主页后接入。',
    i18nSwitch: '中英文切换',
    i18nTodo: '中英文切换暂不启用。该能力可能影响生产 prompt payload 语言边界，需要单独评审后实现。',
    settingsTitle: 'Actanara 设置',
    readingSettings: '读取设置…',
    settingsReadFailed: '读取设置失败: ',
    tabGeneral: '基础',
    tabSchedule: '定时设置',
    tabPaths: '路径设置',
    tabRuntimeSources: '数据源',
    tabExternalTools: '外部工具',
    tabFeatures: '功能设置',
    configFile: '配置文件：',
    saveSettings: '保存设置',
    saving: '保存中…',
    githubSectionNote: '项目主页链接待确认；按钮当前不跳转。',
    llmProviderList: 'LLM Provider 列表',
    llmProviderListNote: 'Provider 下拉列表暂时只包含 MiniMax；后续将扩展 OpenAI-compatible、Anthropic、Gemini 等提供商。',
    cliReserved: 'CLI 预留',
    diaryProjectionRepair: 'Diary projection 与 token usage 修复',
    diaryProjectionRepairNote: '先 dry-run 预览影响范围；执行重建会按日期范围重建 diary markdown documents、sections、period page、period summary，并补齐缺失的 diary hourly token usage events。不会在保存路径设置时自动触发。',
    startDate: '开始日期',
    endDate: '结束日期',
    dryRunPreview: 'Dry-run 预览',
    confirmRebuild: '确认重建',
    refreshRebuildJobs: '刷新重建记录',
    dangerousSqliteRebuild: '危险操作：重建 SQLite 缓存',
    sqliteRebuildNote: '该操作会备份并替换 runtime SQLite read-model cache，然后按当前设置路径和现有源数据重建可再生缓存。无法从当前源重新获取的历史 AI 资产不会出现在新数据库中。',
    previewRebuildPlan: '预览重建计划',
    rebuildSqliteCache: '重建 SQLite 缓存',
    checkingDiarySqlite: '检查 Diary / SQLite 一致性…',
    consistencyFailed: '一致性检查失败: ',
    status: '状态',
    projectionRefreshSuggested: '检测到差异，建议刷新对应日期范围的 Foundation diary markdown projection。',
    sqliteMatchesDisk: '当前 sqlite ready rows 与磁盘相对路径一致。',
    sqliteMissingDisk: 'SQLite 指向但当前目录缺失：',
    diskMissingSqlite: '当前目录新增但 sqlite 未物化：',
    truncatedResults: '结果较多，已截断显示。',
    dateRangeRequired: '请先填写开始日期和结束日期',
    endDateAfterStart: '结束日期必须晚于或等于开始日期',
    confirmDiaryRebuild: (start, end) => `确认重建 ${start} 到 ${end} 的 Diary projections，并修复缺失 token usage events？执行前建议先 dry-run。`,
    generatingDryRun: '生成 dry-run 预览…',
    rebuildingDiaryProjection: '正在重建 Diary projections 与 token usage…',
    rebuildFailed: '重建失败: ',
    executed: '已执行',
    tokenHourlyData: 'Token 小时数据',
    willRepairMissingDates: '将补齐缺失日期',
    tokenRepair: 'Token 修复',
    missingDiskInRange: '范围内 SQLite 指向但磁盘缺失：',
    missingDbInRange: '范围内磁盘存在但 SQLite 缺失：',
    rebuildCompleteNote: '重建完成后已重新检查 Diary / SQLite 一致性，并按 includeUsage 策略修复 token usage events。',
    readingRebuildJobs: '读取重建记录…',
    readRebuildJobsFailed: '读取重建记录失败: ',
    noRebuildJobs: '暂无 Diary projection rebuild 记录。',
    sqliteRebuildPrompt: '危险操作：这会备份并替换当前 SQLite 缓存，可能永久移除无法从当前源重建的历史 AI 资产。若确认继续，请输入：',
    confirmationMismatchCancelled: '确认文本不匹配，已取消。',
    generatingSqlitePlan: '生成 SQLite 重建计划…',
    rebuildingSqlite: '正在重建 SQLite 缓存…',
    sqliteRebuildFailed: 'SQLite 重建失败: ',
    diaryDates: 'Diary dates',
    confirmationTextRequired: '执行需要输入确认文本：',
    usageIngestion: 'Usage ingestion',
    projectionRun: 'Projection run',
    runtimeSourceRetiredNote: '当前保存值为 retired legacy；保存设置会恢复为 foundation。legacy 诊断请使用专用 archive/diagnostic 工具。',
    runtimeSourcesTitle: '读取/写入数据源',
    runtimeSourcesNote: '这些值写入 settings.json 的 runtimeSources。生产 Dashboard 只允许 foundation；legacy 已退役，仅保留给专用迁移/诊断工具。',
    noExternalTools: '暂无 externalTools 设置。',
    externalToolPaths: '外部工具路径',
    externalToolPathsNote: '这些路径写入 settings.json 的 externalTools，并影响 Dashboard 对 OpenClaw、Claude Code、Codex、Gemini CLI、Hermes、OpenCode、Antigravity、Cursor 等历史/当前资料的读取。',
    pipelineSettingsNote: '这些值会影响之后启动的 pipeline 子进程；已经运行中的 pipeline 不会被 retroactively 修改。',
    noStepTimeouts: '暂无 step timeout 配置。',
  },
  en: {
    action: 'Action',
    started: 'Started ',
    completed: 'Completed ',
    backgroundTask: 'Background Task',
    backgroundTaskActionFailed: 'Background task action failed: ',
    running: 'Running',
    recentTasks: 'Recent Tasks',
    sources: 'Sources',
    services: 'Services',
    statusBreakdown: 'Status Breakdown',
    sourceBreakdown: 'Source Breakdown',
    backgroundTaskSourceCount: (count) => `${count} background sources read`,
    noBackgroundTasks: 'No background task records',
    backgroundTasksReadFailed: 'Background task read failed: ',
    backgroundTasksTitle: 'Background Task Monitor',
    readingBackgroundTasks: 'Reading background tasks...',
    choosePeriodFirst: 'Select at least one period first',
    chooseScheduleTime: 'Choose a scheduled generation time',
    noMissingItems: 'No pending items in the current plan.',
    previewFirstItems: 'Previewing only the first 120 pending items; the rest will continue through the background task.',
    pendingItems: 'Pending Items',
    missingDiaries: 'Affected Dates',
    existingDiaries: 'Dates With Data',
    missingSummaries: 'Weekly/Monthly Summaries',
    maxLlmCalls: 'Max Estimated LLM Calls',
    type: 'Type',
    pendingItem: 'Pending Item',
    diary: 'Diary',
    monthlyReport: 'Monthly Report',
    weeklyReport: 'Weekly Report',
    other: 'Other',
    calculatingPlan: 'Calculating plan...',
    dryRunReady: 'Plan preview generated',
    planFailed: 'Plan failed: ',
    dryRunRequiredBeforeQueue: 'Generate a plan preview for the current selection before queueing.',
    dryRunStale: 'The current selection changed. Generate a new plan preview before submitting.',
    submittingBackgroundTask: 'Submitting background task...',
    checkingExistingDiaries: 'Checking existing diary data...',
    overwriteConfirm: (items) => `This will overwrite existing data: ${items}. Confirm?`,
    overwriteCancelled: 'Overwrite regeneration cancelled',
    scheduled: 'Scheduled',
    startedRun: 'Started',
    runDetailsHint: '; check Background Tasks for details.',
    period: 'Periods',
    dailyPipeline: 'Daily Pipeline',
    days: 'days',
    overwriteConfirmed: 'Overwrite regeneration confirmed.',
    historyQueued: 'Historical backfill has been queued. Only one running or scheduled instance is allowed at a time.',
    viewBackgroundTasks: 'View Background Tasks',
    submitFailed: 'Submit failed: ',
    historyBackfillTitle: 'Generate Historical Data',
    historyBackfillSection: 'Historical Backfill',
    historyBackfillNote: 'For users with substantial historical data. The task fills missing diary, SQLite, RAG, and Nova-Task contract items, then generates weekly/monthly aggregates. Weekly/monthly summaries overwrite existing summaries and call the current LLM provider.',
    selectedPeriods: 'Selected Periods',
    choosePeriod: 'Choose Periods',
    runMode: 'Run Mode',
    runNow: 'Run Now',
    runScheduled: 'Schedule',
    scheduledTime: 'Scheduled Time',
    generateSummaries: 'Generate weekly/monthly summaries (overwrites existing summaries)',
    skipReady: 'Skip dates with existing data (full rebuild)',
    overwriteNote: 'When disabled, all selected dates are rebuilt with overwrite confirmation. Selected weekly/monthly summaries are overwritten when summary generation is enabled.',
    estimateFirst: 'Generate a plan preview first',
    dryRunEstimate: 'Plan Preview',
    queueGeneration: 'Queue Generation',
    noSelectedPeriods: 'No periods selected',
    periodPickerTitle: 'Choose Periods',
    periodPickerNote: 'Choose months or weeks from the last six months. Historical completion expands to matching dates, fills missing diaries and daily pipeline data first, then generates period aggregates.',
    cancel: 'Cancel',
    confirmSelection: 'Confirm Selection',
    attentionItems: (count) => `${count} item${Number(count) === 1 ? '' : 's'} need attention`,
    messagesTitle: 'Messages and Action Items',
    severityError: 'Failed',
    severityWarn: 'Needs Attention',
    severityInfo: 'Info',
    read: 'Read',
    message: 'Message',
    openFailed: 'Open failed: ',
    actionSubmitted: 'Action submitted',
    actionFailed: 'Action failed: ',
    noMessages: 'No messages need attention',
    readingMessages: 'Reading messages...',
    onboardingTitle: 'New User Readiness',
    onboardingNote: 'Read-only panel for minimum deployment, optional subsystem dependencies, runtime, nova-RAG, scheduler, and resource profile. It does not install, write settings, or register system tasks.',
    refreshOnboarding: 'Refresh Onboarding Status',
    notReadYet: 'Not read yet',
    readingOnboardingReadiness: 'Reading onboarding readiness...',
    readingOnboardingPlan: 'Reading onboarding plan...',
    onboardingReadFailed: 'Onboarding read failed: ',
    noSettingsAuthority: 'No settings authority inventory.',
    settingsAuthorityTitle: 'Settings Authority Inventory',
    settingsAuthorityNote: 'Read-only view for settings.json, env overrides, derived/default values, and manual/auto status. It does not write configuration.',
    persistentWrite: 'Persistent Write',
    envSemantics: 'Env Semantics',
    defaultManual: 'Default/Manual',
    secret: 'Secret',
    writableVia: 'Writable via: ',
    productLocalization: 'Product and Localization',
    dashboardService: 'Dashboard Service',
    dashboardRestartNote: 'These values are saved to settings.json. A running Dashboard LaunchAgent must be restarted before host/port/python/appDir startup parameters fully apply.',
    restartCommand: 'Restart command: ',
    copyCommand: 'Copy Command',
    systemSchedulerMode: 'System Scheduler Mode',
    enableSystemScheduler: 'Enable system scheduler',
    timezone: 'Timezone',
    dailyPipelineTime: 'Daily Pipeline Time',
    dashboardAggregationTime: 'Dashboard Aggregation Time',
    systemTimerProvider: 'System Timer Provider',
    systemTimerLabel: 'System Timer Label',
    systemTimerNote: 'System timers are not written automatically when settings are saved. Installation creates two user-level launchd jobs: daily pipeline and Dashboard Foundation aggregation, with backup/uninstall paths.',
    previewSystemTimer: 'Preview System Timers',
    installUpdate: 'Install / Update',
    uninstall: 'Uninstall',
    autoRefreshTargets: 'Auto Refresh Targets',
    currentDaySnapshot: 'Current Day Snapshot',
    currentWeekSnapshot: 'Current Week Snapshot',
    currentMonthSnapshot: 'Current Month Snapshot',
    externalAgentMode: 'External Agent Mode',
    enableExternalAgentMode: 'Enable external agent mode',
    externalAgentNote: 'After sending to an external agent, that agent manages system timers. Dashboard does not take over full production pipeline execution.',
    prompt: 'Prompt',
    currentSelection: 'Current Selection',
    editedValue: 'Edited Value',
    readingRuntimePath: 'Reading current runtime path...',
    runtimePathReadFailed: 'Current path read failed: ',
    processing: 'Processing...',
    pathOperationFailed: 'Path operation failed: ',
    runtimePathConfirmationPrompt: 'Enter the confirmation phrase to switch or initialize the Actanara runtime path: ',
    readingPath: 'Reading path...',
    pathReadFailed: 'Path read failed: ',
    parentDirectory: 'Parent Directory',
    chooseCurrentDirectory: 'Choose Current Directory',
    browse: 'Browse',
    userPaths: 'User Paths',
    userPathsNote: 'These paths affect newly generated artifacts or future read locations. Saving the Diary path updates the runtime manifest; existing SQLite content does not move files automatically.',
    snapshotsPath: 'Snapshots Path',
    diaryPath: 'Diary Path',
    reportsPath: 'Reports Path',
    archivesPath: 'Archives Path',
    taskIntelligencePath: 'Task Intelligence Path',
    taskBoardPath: 'Task Board Path',
    ragIndexPath: 'nova-RAG Index Path',
    runtimeHomeNote: 'Validating or switching runtime home updates the current Actanara selection. Import legacy attempts to copy migratable assets from the legacy diary root.',
    refreshRuntimePath: 'Refresh Runtime Path',
    validateRuntimePath: 'Validate Runtime Path',
    useRuntimePath: 'Use This Runtime Path',
    initializeRuntimePath: 'Initialize Runtime Path',
    checkDiarySqlite: 'Check Diary / SQLite Consistency',
    featureSettingsNote: 'Only product-level switches are exposed here. LLM generation, task auditing, and Embedding Server behavior are default runtime or subsystem-internal configuration, not ordinary feature switches.',
    featureDailyPipeline: 'Daily Pipeline',
    featureDashboard: 'Dashboard Service',
    saved: 'Saved ',
    restartRequiredSaved: '; startup parameter changes require a Dashboard restart to fully apply. Command: ',
    saveFailed: 'Save failed: ',
    readingSystemTimerPreview: 'Reading system timer preview...',
    previewFailed: 'Preview failed: ',
    installTimerPrompt: 'Enter the confirmation phrase to install system timers: ',
    uninstallTimerPrompt: 'Enter the confirmation phrase to uninstall system timers: ',
    installCancelledMismatch: 'Install cancelled: confirmation phrase did not match.',
    uninstallCancelledMismatch: 'Uninstall cancelled: confirmation phrase did not match.',
    installingSystemTimer: 'Installing system timers...',
    uninstallingSystemTimer: 'Uninstalling system timers...',
    installedJobs: (count) => `Installed ${count} launchd job${Number(count) === 1 ? '' : 's'}. Backup: `,
    uninstalledJobs: (count) => `Uninstalled ${count} launchd job${Number(count) === 1 ? '' : 's'}. Backup: `,
    installFailed: 'Install failed: ',
    uninstallFailed: 'Uninstall failed: ',
    githubProject: 'GitHub Project Home',
    githubTodo: 'GitHub link is not configured yet. This button is reserved until the project home is confirmed.',
    i18nSwitch: 'Language Switch',
    i18nTodo: 'Language switching is not enabled yet. It may affect production prompt payload language boundaries and requires a separate review.',
    settingsTitle: 'Actanara Settings',
    readingSettings: 'Reading settings...',
    settingsReadFailed: 'Settings read failed: ',
    tabGeneral: 'General',
    tabSchedule: 'Schedule',
    tabPaths: 'Paths',
    tabRuntimeSources: 'Data Sources',
    tabExternalTools: 'External Tools',
    tabFeatures: 'Features',
    configFile: 'Config file: ',
    saveSettings: 'Save Settings',
    saving: 'Saving...',
    githubSectionNote: 'Project home link is not confirmed; the button does not navigate yet.',
    llmProviderList: 'LLM Provider List',
    llmProviderListNote: 'The provider dropdown currently includes only MiniMax; OpenAI-compatible, Anthropic, Gemini, and other providers will be added later.',
    cliReserved: 'CLI Reserved',
    diaryProjectionRepair: 'Diary Projection and Token Usage Repair',
    diaryProjectionRepairNote: 'Run a dry-run first to preview impact. Rebuild regenerates diary markdown documents, sections, period pages, period summaries, and missing diary hourly token usage events across the date range. Saving path settings does not trigger this automatically.',
    startDate: 'Start Date',
    endDate: 'End Date',
    dryRunPreview: 'Dry-run Preview',
    confirmRebuild: 'Confirm Rebuild',
    refreshRebuildJobs: 'Refresh Rebuild Jobs',
    dangerousSqliteRebuild: 'Danger: Rebuild SQLite Cache',
    sqliteRebuildNote: 'This backs up and replaces the runtime SQLite read-model cache, then rebuilds reproducible cache data from current paths and source data. Historical AI assets that cannot be fetched from current sources will not appear in the new database.',
    previewRebuildPlan: 'Preview Rebuild Plan',
    rebuildSqliteCache: 'Rebuild SQLite Cache',
    checkingDiarySqlite: 'Checking Diary / SQLite consistency...',
    consistencyFailed: 'Consistency check failed: ',
    status: 'Status',
    projectionRefreshSuggested: 'Differences detected; refresh the Foundation diary markdown projection for the matching date range.',
    sqliteMatchesDisk: 'Current sqlite ready rows match disk relative paths.',
    sqliteMissingDisk: 'SQLite points to files missing from the current directory: ',
    diskMissingSqlite: 'Current directory has files not materialized in sqlite: ',
    truncatedResults: 'Results truncated.',
    dateRangeRequired: 'Fill in both start and end dates first',
    endDateAfterStart: 'End date must be later than or equal to start date',
    confirmDiaryRebuild: (start, end) => `Rebuild Diary projections from ${start} to ${end} and repair missing token usage events? Run a dry-run first.`,
    generatingDryRun: 'Generating dry-run preview...',
    rebuildingDiaryProjection: 'Rebuilding Diary projections and token usage...',
    rebuildFailed: 'Rebuild failed: ',
    executed: 'Executed',
    tokenHourlyData: 'Token Hourly Data',
    willRepairMissingDates: 'will repair missing dates',
    tokenRepair: 'Token Repair',
    missingDiskInRange: 'SQLite references missing files in range: ',
    missingDbInRange: 'Disk files missing from SQLite in range: ',
    rebuildCompleteNote: 'After rebuild, Diary / SQLite consistency was rechecked and token usage events were repaired according to includeUsage policy.',
    readingRebuildJobs: 'Reading rebuild jobs...',
    readRebuildJobsFailed: 'Rebuild job read failed: ',
    noRebuildJobs: 'No Diary projection rebuild records.',
    sqliteRebuildPrompt: 'Danger: this backs up and replaces the current SQLite cache and may permanently remove historical AI assets that cannot be rebuilt from current sources. To continue, enter: ',
    confirmationMismatchCancelled: 'Confirmation text did not match; cancelled.',
    generatingSqlitePlan: 'Generating SQLite rebuild plan...',
    rebuildingSqlite: 'Rebuilding SQLite cache...',
    sqliteRebuildFailed: 'SQLite rebuild failed: ',
    diaryDates: 'Diary dates',
    confirmationTextRequired: 'Execution requires confirmation text: ',
    usageIngestion: 'Usage ingestion',
    projectionRun: 'Projection run',
    runtimeSourceRetiredNote: 'The saved value is retired legacy; saving settings restores foundation. Use dedicated archive/diagnostic tools for legacy diagnostics.',
    runtimeSourcesTitle: 'Read/Write Data Sources',
    runtimeSourcesNote: 'These values are written to settings.json runtimeSources. Production Dashboard allows only foundation; legacy is retired and kept only for dedicated migration/diagnostic tools.',
    noExternalTools: 'No externalTools settings.',
    externalToolPaths: 'External Tool Paths',
    externalToolPathsNote: 'These paths are written to settings.json externalTools and affect Dashboard reads of historical/current data for OpenClaw, Claude Code, Codex, Gemini CLI, Hermes, OpenCode, Antigravity, Cursor, and related tools.',
    pipelineSettingsNote: 'These values affect pipeline child processes started after this change; already-running pipelines are not modified retroactively.',
    noStepTimeouts: 'No step timeout configuration.',
  },
};

const LLM_UI_TEXT = {
  zh: {
    title: '日记生成 LLM Provider',
    readingProvider: '读取 Provider…',
    readProviderFailed: '读取 Provider 失败: ',
    manualOverride: (value, drift) => `当前为手动覆盖；自动建议值 ${value}${drift ? '，与当前值不同' : ''}`,
    autoGate: (value) => `当前随模型 context 自动更新；自动建议值 ${value}`,
    cancel: '取消',
    testAvailability: '检测可用性',
    saveProvider: '保存 Provider',
    provider: '提供商',
    model: '模型',
    apiType: 'API 类型',
    pipelineConcurrency: 'Pipeline 并发',
    requestTimeout: '请求 Timeout',
    autoSuggestion: '自动建议',
    useAutoValue: '使用自动值',
    apiKey: '密钥',
    savedKeepBlank: '已保存，留空保持不变',
    notConfigured: '未配置',
    testBeforeSave: '可在保存前检测当前表单配置；检测不会保存密钥。',
    regularModeNote: '常规模式只需选择提供商/模型并填写 key；Endpoint、模型 ID、context window 由系统目录提供。Custom 才允许手动填写完整参数。不会改变受保护 prompt payload。',
    statusNeedsTransport: '待支持 transport',
    statusAuthLocal: '需自定义/登录',
    statusCustom: '自定义',
    statusAvailable: '可用',
    endpointMissing: 'Endpoint: 未内置',
    saving: '保存中…',
    saved: '已保存 ',
    saveFailed: '保存失败: ',
    testing: '检测中…',
    testPassed: '检测通过：',
    testFailedFull: '检测失败：',
    testFailed: '检测失败: ',
  },
  en: {
    title: 'Diary Generation LLM Provider',
    readingProvider: 'Reading Provider...',
    readProviderFailed: 'Provider read failed: ',
    manualOverride: (value, drift) => `Manual override; automatic recommendation ${value}${drift ? ', differs from current value' : ''}`,
    autoGate: (value) => `Auto-updates from model context; automatic recommendation ${value}`,
    cancel: 'Cancel',
    testAvailability: 'Test Availability',
    saveProvider: 'Save Provider',
    provider: 'Provider',
    model: 'Model',
    apiType: 'API Type',
    pipelineConcurrency: 'Pipeline Concurrency',
    requestTimeout: 'Request Timeout',
    autoSuggestion: 'Auto Suggestion',
    useAutoValue: 'Use Auto Value',
    apiKey: 'API Key',
    savedKeepBlank: 'Saved; leave blank to keep unchanged',
    notConfigured: 'Not configured',
    testBeforeSave: 'Test the current form before saving; the test does not save the API key.',
    regularModeNote: 'In regular mode, choose provider/model and enter a key. Endpoint, model ID, and context window come from the system catalog. Custom mode allows full manual parameters. Protected prompt payloads are unchanged.',
    statusNeedsTransport: 'transport pending',
    statusAuthLocal: 'custom/login required',
    statusCustom: 'custom',
    statusAvailable: 'available',
    endpointMissing: 'Endpoint: not built in',
    saving: 'Saving...',
    saved: 'Saved ',
    saveFailed: 'Save failed: ',
    testing: 'Testing...',
    testPassed: 'Test passed: ',
    testFailedFull: 'Test failed: ',
    testFailed: 'Test failed: ',
  },
};

const AI_ASSETS_TEXT = {
  zh: {
    loading: '⏳ 加载中…',
    refresh: '🔄 刷新',
    updatedAt: '更新于 ',
    loadFailed: '加载失败: ',
    retry: '🔄 重试',
    submitting: '提交中…',
    queued: '排队中…',
    updating: '更新中…',
    backgroundUpdate: '后台更新',
    retryUpdate: '重试更新',
    updateFailed: 'AI Assets 更新失败: ',
    totalTokens: '总消耗 Token',
    totalMessages: '累计消息数',
    activeSystems: '活跃系统',
    agentInstances: 'Agent 实例',
    lookbackDays: '追溯天数',
    countUnit: '个',
    dayUnit: '天',
    cumulativeUsage: '累计消耗',
    todayUsage: '今日消耗',
    localUsageUnavailable: '本地无可靠用量数据',
    localUsagePartial: '本地可见用量可能不完整',
    allTimeTokens: 'All-Time Tokens',
    tokenUnit: 'tokens',
    servicesUnit: 'services',
    firstActive: '首活跃: ',
    lastActive: '末活跃: ',
    diaryCount: '日记数',
    firstDiary: '首篇',
    lastDiary: '末篇',
    totalWords: '总字数',
    sessionFiles: 'Session 文件',
    totalSize: '总大小',
    diaryMemory: '日记 Memory',
    dailyNotes: '每日笔记',
    activeIndexFiles: '活跃索引文件',
    indexEntries: '索引数据条目',
    activeIndexSize: '活跃索引大小',
    unknown: '未知',
    indexUpdatedAt: '索引更新时间',
    status: '状态',
    total: '总数',
    success: '成功',
    failed: '失败',
    successRate: '成功率',
    noDeviceData: '暂无设备数据',
    readingFoundationJobs: '读取 Foundation job 状态…',
    readingCompleteness: '读取 projection completeness…',
    latestJobsNoFailures: '最近 job 无失败记录',
    noFoundationJobs: '尚无 Foundation refresh job',
    recentRead: '最近读取 ',
    readFailed: '读取失败: ',
    refreshFailed: 'Foundation refresh 失败: ',
    submittedRun: '已提交 Run #',
    backfillFailed: 'Backfill 失败: ',
    readingFile: '正在读取文件...',
    savePrompt: '输入确认短语以保存文件：',
    saveCancelled: '❌ 保存已取消：确认短语不匹配',
    saveSuccess: '✅ 保存成功',
    saveFailed: '❌ 保存失败: ',
    systemError: '❌ 系统错误: ',
    backupLabel: '已备份',
    loadingFile: '加载中…',
    loadFileFailed: '❌ 加载失败: ',
    fileWillBeCreated: '文件尚不存在，保存后会创建',
    createFileTitle: (agentName, fileName) => `${agentName} · 创建 ${fileName}`,
    toolStorage: '工具占用空间',
    artifactDetails: 'Actanara 产物明细',
    noStorageData: '暂无存储数据',
    noAgentData: '暂无 Agent 数据',
    levelLabels: { global: '全局配置', workspace: '工作区 / 项目', agent: 'Agent', session: '会话记录' },
    itemUnit: '项',
    fileGroupDescriptions: {
      context: '会进入或影响模型上下文的长期指令',
      config: '权限、MCP、插件、环境等运行配置',
      tools: 'Slash commands、工具提示词和插件参考文件',
    },
    fileGroupTitles: {
      context: 'Context Instructions',
      config: 'Runtime Config',
      tools: 'Commands / Plugin Assets',
    },
    fileKindLabels: {
      context: 'Context',
      config: 'Config',
      command: 'Command',
      reference: 'Reference',
      skill: 'Skill',
      memory: 'Memory',
    },
    workspaceBuckets: {
      current: { title: 'Current Project', desc: '当前 dashboard 项目，优先编辑' },
      project: { title: 'Project Workspaces', desc: '来自真实项目目录的历史工作区' },
      home: { title: 'CLI Home / General', desc: '从 home 目录启动的通用会话' },
      general: { title: 'General / Broad Directory', desc: '从 SSD、home 等泛目录启动的非标准工作区' },
      external: { title: 'External / Probe', desc: 'Codex Desktop、CodexBar 或临时探测目录' },
    },
    messages: '消息',
    sessions: 'Sessions',
    lastActive: '最后活跃',
    keyFilesHint: (count) => `${count} 个关键文件 — 点击查看`,
    modelLabel: '模型',
    sourceLabel: '来源',
    noKeyFiles: '无关键文件',
    missingFile: 'Missing',
    documentFallback: '文档',
    fileFallback: '文件',
    profileLabel: 'Profile',
    linesUnit: '行',
    skillsLibrary: '技能库',
    skillSearchPlaceholder: '🔍 搜索所有工具的 Skills...',
    noSkillsData: '暂无 Skills 数据',
    noMatches: '无匹配结果',
    noDescription: '暂无描述',
    sourceKindLabel: '来源',
    editSkill: '✏️ 编辑 Skill',
    noToolConfigData: '暂无工具配置数据',
    detectedTools: '已检测工具',
    listeningServices: '监听服务',
    lastChecked: '最近检测',
    noListeningPorts: '无监听端口',
    versionUnknown: '版本未识别',
    runPath: '运行',
    configPath: '配置',
    executablePath: '执行',
    configUpdated: '配置更新',
    checkedAt: '检测时间',
    detecting: '检测并刷新缓存中',
    rediscover: '检测并刷新缓存',
    pathDetectFailed: '路径检测失败',
    toolRediscovery: '工具重新检测',
    detectionFailed: '检测失败',
    writeSettings: '写入设置',
    matched: '已匹配',
    noSupportedToolDirs: '未检测到支持工具目录',
    rediscoveryNote: '路径建议只核对目录标记；本次操作会刷新工具配置缓存。写入设置需要点击单条建议或使用手动添加。',
    tool: '工具',
    instance: '实例',
    path: '路径',
    action: '操作',
    manualAddTool: '手动添加工具',
    instanceName: '实例名称',
    instancePlaceholder: '留空使用默认；多 OpenClaw 可填 openclaw-2',
    addTool: '添加工具',
    writing: '写入中…',
    writeFailed: '写入失败',
    writtenRefreshing: (name) => `已写入 ${name}，正在刷新…`,
    written: (name) => `已写入 ${name}`,
    pathRediscoveryFailed: '路径重检测失败',
    toolConfigRefreshFailed: '工具配置刷新失败',
  },
  en: {
    loading: '⏳ Loading...',
    refresh: '🔄 Refresh',
    updatedAt: 'Updated at ',
    loadFailed: 'Load failed: ',
    retry: '🔄 Retry',
    submitting: 'Submitting...',
    queued: 'Queued...',
    updating: 'Updating...',
    backgroundUpdate: 'Background Update',
    retryUpdate: 'Retry Update',
    updateFailed: 'AI Assets update failed: ',
    totalTokens: 'Total Tokens',
    totalMessages: 'Total Messages',
    activeSystems: 'Active Systems',
    agentInstances: 'Agent Instances',
    lookbackDays: 'Lookback Days',
    countUnit: '',
    dayUnit: 'days',
    cumulativeUsage: 'Cumulative Usage',
    todayUsage: 'Today Usage',
    localUsageUnavailable: 'Reliable usage is not available locally',
    localUsagePartial: 'Locally visible usage may be incomplete',
    allTimeTokens: 'All-Time Tokens',
    tokenUnit: 'tokens',
    servicesUnit: 'services',
    firstActive: 'First active: ',
    lastActive: 'Last active: ',
    diaryCount: 'Diaries',
    firstDiary: 'First',
    lastDiary: 'Latest',
    totalWords: 'Total Words',
    sessionFiles: 'Session Files',
    totalSize: 'Total Size',
    diaryMemory: 'Diary Memory',
    dailyNotes: 'Daily Notes',
    activeIndexFiles: 'Active Index Files',
    indexEntries: 'Index Entries',
    activeIndexSize: 'Active Index Size',
    unknown: 'Unknown',
    indexUpdatedAt: 'Index Updated',
    status: 'Status',
    total: 'Total',
    success: 'Success',
    failed: 'Failed',
    successRate: 'Success Rate',
    noDeviceData: 'No device data',
    readingFoundationJobs: 'Reading Foundation job status...',
    readingCompleteness: 'Reading projection completeness...',
    latestJobsNoFailures: 'No recent failed jobs',
    noFoundationJobs: 'No Foundation refresh jobs yet',
    recentRead: 'Last read ',
    readFailed: 'Read failed: ',
    refreshFailed: 'Foundation refresh failed: ',
    submittedRun: 'Submitted Run #',
    backfillFailed: 'Backfill failed: ',
    readingFile: 'Reading file...',
    savePrompt: 'Enter confirmation phrase to save file: ',
    saveCancelled: '❌ Save cancelled: confirmation phrase mismatch',
    saveSuccess: '✅ Saved',
    saveFailed: '❌ Save failed: ',
    systemError: '❌ System error: ',
    backupLabel: 'backed up',
    loadingFile: 'Loading...',
    loadFileFailed: '❌ Load failed: ',
    fileWillBeCreated: 'File does not exist yet; saving will create it',
    createFileTitle: (agentName, fileName) => `${agentName} · Create ${fileName}`,
    toolStorage: 'Tool Storage',
    artifactDetails: 'Actanara Artifact Details',
    noStorageData: 'No storage data',
    noAgentData: 'No Agent data',
    levelLabels: { global: 'Global Config', workspace: 'Workspace / Project', agent: 'Agent', session: 'Session Records' },
    itemUnit: 'items',
    fileGroupDescriptions: {
      context: 'Long-lived instructions that enter or influence model context',
      config: 'Runtime permissions, MCP, plugins, environment, and related configuration',
      tools: 'Slash commands, tool prompts, and plugin reference files',
    },
    fileGroupTitles: {
      context: 'Context Instructions',
      config: 'Runtime Config',
      tools: 'Commands / Plugin Assets',
    },
    fileKindLabels: {
      context: 'Context',
      config: 'Config',
      command: 'Command',
      reference: 'Reference',
      skill: 'Skill',
      memory: 'Memory',
    },
    workspaceBuckets: {
      current: { title: 'Current Project', desc: 'Current dashboard project, preferred for editing' },
      project: { title: 'Project Workspaces', desc: 'Historical workspaces from real project directories' },
      home: { title: 'CLI Home / General', desc: 'General sessions launched from the home directory' },
      general: { title: 'General / Broad Directory', desc: 'Non-standard workspaces launched from broad SSD or home directories' },
      external: { title: 'External / Probe', desc: 'Codex Desktop, CodexBar, or temporary probe directories' },
    },
    messages: 'Messages',
    sessions: 'Sessions',
    lastActive: 'Last active',
    keyFilesHint: (count) => `${count} key files - click to view`,
    modelLabel: 'Model',
    sourceLabel: 'Source',
    noKeyFiles: 'No key files',
    missingFile: 'Missing',
    documentFallback: 'Document',
    fileFallback: 'File',
    profileLabel: 'Profile',
    linesUnit: 'lines',
    skillsLibrary: 'Skill Library',
    skillSearchPlaceholder: '🔍 Search all tool Skills...',
    noSkillsData: 'No Skills data',
    noMatches: 'No matches',
    noDescription: 'No description',
    sourceKindLabel: 'Source',
    editSkill: '✏️ Edit Skill',
    noToolConfigData: 'No tool configuration data',
    detectedTools: 'Detected Tools',
    listeningServices: 'Listening Services',
    lastChecked: 'Last Checked',
    noListeningPorts: 'No listening ports',
    versionUnknown: 'Version not detected',
    runPath: 'Run',
    configPath: 'Config',
    executablePath: 'Executable',
    configUpdated: 'Config Updated',
    checkedAt: 'Checked At',
    detecting: 'Detecting and refreshing cache',
    rediscover: 'Detect & Refresh Cache',
    pathDetectFailed: 'Path detection failed',
    toolRediscovery: 'Tool Rediscovery',
    detectionFailed: 'Detection failed',
    writeSettings: 'Write Settings',
    matched: 'Matched',
    noSupportedToolDirs: 'No supported tool directories detected',
    rediscoveryNote: 'Path suggestions only check directory markers. This action refreshes the tool configuration cache. Write settings from a suggestion or add one manually.',
    tool: 'Tool',
    instance: 'Instance',
    path: 'Path',
    action: 'Action',
    manualAddTool: 'Add Tool Manually',
    instanceName: 'Instance Name',
    instancePlaceholder: 'Leave blank for default; for multiple OpenClaw instances use openclaw-2',
    addTool: 'Add Tool',
    writing: 'Writing...',
    writeFailed: 'Write failed',
    writtenRefreshing: (name) => `Wrote ${name}; refreshing...`,
    written: (name) => `Wrote ${name}`,
    pathRediscoveryFailed: 'Path rediscovery failed',
    toolConfigRefreshFailed: 'Tool configuration refresh failed',
  },
};

function dashboardLanguageProfile(value) {
  const raw = String(value || ACTANARA_PIPELINE_LANGUAGE_PROFILE || 'zh').toLowerCase();
  return raw.startsWith('en') ? 'en' : 'zh';
}

function dashboardText(profile) {
  return DASHBOARD_TEXT[dashboardLanguageProfile(profile)];
}

function dashboardShellText(profile) {
  return DASHBOARD_SHELL_TEXT[dashboardLanguageProfile(profile)];
}

function foundationText(profile) {
  return FOUNDATION_TEXT[dashboardLanguageProfile(profile)];
}

function ragUiText(profile) {
  return RAG_UI_TEXT[dashboardLanguageProfile(profile)];
}

function operatorText(profile) {
  return OPERATOR_UI_TEXT[dashboardLanguageProfile(profile)];
}

function llmUiText(profile) {
  return LLM_UI_TEXT[dashboardLanguageProfile(profile)];
}

function aiAssetsText(profile) {
  return AI_ASSETS_TEXT[dashboardLanguageProfile(profile)];
}

function applyStaticDashboardText(profile) {
  const labels = { ...dashboardText(profile), ...dashboardShellText(profile), ...aiAssetsText(profile) };
  document.documentElement.lang = dashboardLanguageProfile(profile) === 'en' ? 'en-US' : 'zh-CN';
  if (labels.documentTitle) document.title = labels.documentTitle;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.dataset.i18n;
    if (key && Object.prototype.hasOwnProperty.call(labels, key)) {
      if (el.id === 'sseStatus' && !/连接中|Connecting/.test(el.textContent || '')) return;
      el.textContent = labels[key];
    }
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.dataset.i18nTitle;
    if (key && Object.prototype.hasOwnProperty.call(labels, key)) {
      el.title = labels[key];
    }
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.dataset.i18nPlaceholder;
    if (key && Object.prototype.hasOwnProperty.call(labels, key)) {
      el.placeholder = labels[key];
    }
  });
  document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
    const key = el.dataset.i18nAriaLabel;
    if (key && Object.prototype.hasOwnProperty.call(labels, key)) {
      el.setAttribute('aria-label', labels[key]);
    }
  });
  renderSseConnectionStatus();
}

function rememberDashboardSettings(settings) {
  const pipeline = settings && settings.pipeline ? settings.pipeline : {};
  ACTANARA_PIPELINE_LANGUAGE_PROFILE = dashboardLanguageProfile(pipeline.languageProfile);
  ACTANARA_SETTINGS_LOADED = true;
  applyStaticDashboardText(ACTANARA_PIPELINE_LANGUAGE_PROFILE);
}

async function ensureDashboardLanguageProfile() {
  if (ACTANARA_SETTINGS_LOADED) return ACTANARA_PIPELINE_LANGUAGE_PROFILE;
  try {
    const res = await fetch('/api/settings');
    if (res.ok) rememberDashboardSettings(await res.json());
  } catch (e) {}
  return ACTANARA_PIPELINE_LANGUAGE_PROFILE;
}

async function refreshBackgroundTaskButton() {
  try {
    const res = await fetch('/api/background-tasks?limit=30');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    BACKGROUND_TASK_STATE = await res.json();
    renderBackgroundTaskButton(BACKGROUND_TASK_STATE);
  } catch (e) {
    renderBackgroundTaskButton({activeCount: 0, tasks: [], error: e.message});
  }
}

function renderBackgroundTaskButton(state) {
  const button = document.getElementById('taskMonitorButton');
  const count = document.getElementById('taskMonitorCount');
  if (!button || !count) return;
  const labels = foundationText();
  const active = Number(state.activeCount || 0);
  button.classList.toggle('has-active', active > 0);
  count.textContent = String(active);
  button.title = active > 0 ? labels.activeBackgroundTasks(active) : labels.backgroundTasks;
}

function backgroundTaskStatusLabel(status) {
  const labels = foundationText();
  return labels.statusLabels[status] || status || labels.statusLabels.unknown;
}

function formatBackgroundTaskTime(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(0, 19);
}

function renderBackgroundTaskItem(task) {
  const labels = operatorText();
  const status = String(task.status || 'unknown');
  const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
  const error = task.errorSummary ? '<div class="task-monitor-error">' + escapeHtml(task.errorSummary) + '</div>' : '';
  const actions = Array.isArray(task.actions) ? task.actions : [];
  const actionHtml = actions.length ? '<div class="task-monitor-actions">' + actions.map(action => {
    const label = escapeHtml(action.label || labels.action);
    if (action.kind === 'disabled') return '<button type="button" class="wr-export-btn secondary" disabled>' + label + '</button>';
    const encoded = encodeURIComponent(JSON.stringify(action));
    return '<button type="button" class="wr-export-btn secondary" onclick="handleBackgroundTaskAction(decodeURIComponent(\'' + encoded + '\'))">' + label + '</button>';
  }).join('') + '</div>' : '';
  const meta = [
    task.source || 'unknown',
    labels.started + formatBackgroundTaskTime(task.startedAt),
    labels.completed + formatBackgroundTaskTime(task.completedAt)
  ].join(' · ');
  return '<div class="task-monitor-item">' +
    '<div class="task-monitor-head"><div class="task-monitor-title">' + escapeHtml(task.title || task.id || labels.backgroundTask) + '</div><span class="task-monitor-status ' + escapeHtml(status) + '">' + escapeHtml(backgroundTaskStatusLabel(status)) + '</span></div>' +
    '<div class="task-monitor-subtitle">' + escapeHtml(task.subtitle || '') + '</div>' +
    '<div class="settings-progress"><div class="settings-progress-bar ' + (status === 'failed' ? 'failed' : '') + '" style="width:' + progress + '%"></div></div>' +
    '<div class="task-monitor-meta">' + escapeHtml(meta) + '</div>' +
    error +
    actionHtml +
    '</div>';
}

function renderBackgroundTaskBreakdown(items, labels) {
  return Object.entries(items || {}).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([key, value]) =>
    '<span class="task-monitor-chip"><b>' + escapeHtml(backgroundTaskStatusLabel(key)) + '</b>' + escapeHtml(String(value)) + '</span>'
  ).join('');
}

function aaRagHealthText(health) {
  const en = dashboardLanguageProfile() === 'en';
  const labels = {
    ready: en ? 'Ready' : '可检索',
    'index-only': en ? 'Index ready; server stopped' : '索引就绪；服务未运行',
    'server-only': en ? 'Server running; index missing' : '服务运行；索引缺失',
    missing: en ? 'Not ready' : '未就绪',
    unknown: en ? 'Unknown' : '未知',
  };
  return labels[health] || labels.unknown;
}

function renderAaRagStatus(rag, labels) {
  const health = rag.health || ((rag.indexFiles || 0) > 0 ? 'index-only' : 'missing');
  const metrics = [
    [labels.activeIndexFiles, rag.indexFiles || 0],
    [labels.indexEntries, (rag.entries || 0).toLocaleString()],
    [labels.activeIndexSize, (rag.sizeMB || 0) + ' MB'],
  ].map(r => '<div class="aa-rag-metric"><span>' + escapeHtml(r[0]) + '</span><b>' + escapeHtml(String(r[1])) + '</b></div>').join('');
  const rows = [
    ['Embedding Server', rag.embeddingStatus || labels.unknown],
    [labels.indexUpdatedAt, rag.updatedAt || '—'],
    ['Source', rag.source || '—'],
  ].map(r => '<div class="aa-info-row"><span>' + escapeHtml(r[0]) + '</span><span>' + escapeHtml(r[1]) + '</span></div>').join('');
  return '<div class="aa-rag-card aa-rag-' + escapeHtml(health) + '">' +
    '<div class="aa-rag-head"><span class="aa-rag-status"><span class="aa-rag-dot"></span>' + escapeHtml(aaRagHealthText(health)) + '</span><span class="aa-rag-source">v2</span></div>' +
    '<div class="aa-rag-metrics">' + metrics + '</div>' +
    rows +
    '</div>';
}

async function handleBackgroundTaskAction(rawAction) {
  let action = rawAction;
  if (typeof rawAction === 'string') {
    try { action = JSON.parse(rawAction); } catch (e) { return; }
  }
  if (!action || action.kind !== 'apiPost' || !action.url) return;
  if (action.confirm && !confirm(action.confirm)) return;
  try {
    const res = await fetch(action.url, {method: 'POST'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (action.refreshBackgroundTasks) {
      await refreshBackgroundTaskButton();
      await refreshBackgroundTasksModal();
    }
  } catch (e) {
    alert(operatorText().backgroundTaskActionFailed + e.message);
  }
}

function renderBackgroundTasksModal(state) {
  const labels = operatorText();
  const tasks = Array.isArray(state.tasks) ? state.tasks : [];
  const sources = state.sources || {};
  const stateSummary = state.summary || {};
  const byStatus = stateSummary.byStatus || {};
  const bySource = stateSummary.bySource || {};
  const summary = '<div class="task-monitor-summary">' +
    '<div class="task-monitor-summary-card"><span>' + escapeHtml(labels.running) + '</span><b>' + escapeHtml(state.activeCount || 0) + '</b></div>' +
    '<div class="task-monitor-summary-card"><span>' + escapeHtml(labels.recentTasks) + '</span><b>' + escapeHtml(tasks.length) + '</b></div>' +
    '<div class="task-monitor-summary-card"><span>' + escapeHtml(labels.services) + '</span><b>' + escapeHtml(stateSummary.services || (state.services || []).length || 0) + '</b></div>' +
    '</div>';
  const breakdown = '<div class="task-monitor-breakdown">' +
    '<div><span>' + escapeHtml(labels.statusBreakdown) + '</span><div>' + (renderBackgroundTaskBreakdown(byStatus, labels) || '<em>—</em>') + '</div></div>' +
    '<div><span>' + escapeHtml(labels.sourceBreakdown) + '</span><div>' + (renderBackgroundTaskBreakdown(bySource, labels) || '<em>—</em>') + '</div></div>' +
    '<small>' + escapeHtml(labels.backgroundTaskSourceCount(Object.values(sources).filter(Boolean).length)) + '</small>' +
    '</div>';
  const body = tasks.length
    ? '<div class="task-monitor-list">' + tasks.map(renderBackgroundTaskItem).join('') + '</div>'
    : '<div class="fo-empty">' + escapeHtml(labels.noBackgroundTasks) + '</div>';
  return summary + breakdown + body;
}

async function refreshBackgroundTasksModal() {
  const body = document.getElementById('modal-body');
  if (!body) return;
  try {
    const res = await fetch('/api/background-tasks?limit=30');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    BACKGROUND_TASK_STATE = await res.json();
    renderBackgroundTaskButton(BACKGROUND_TASK_STATE);
    body.innerHTML = renderBackgroundTasksModal(BACKGROUND_TASK_STATE);
  } catch (e) {
    body.innerHTML = '<div class="fo-job-error">' + escapeHtml(operatorText().backgroundTasksReadFailed + e.message) + '</div>';
  }
}

async function openBackgroundTasksModal() {
  const labels = operatorText();
  openModal(labels.backgroundTasksTitle, '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingBackgroundTasks) + '</span></div>');
  if (backgroundTasksTimer) clearInterval(backgroundTasksTimer);
  await refreshBackgroundTasksModal();
  backgroundTasksTimer = setInterval(refreshBackgroundTasksModal, 5000);
}

function historyBackfillMonthValue(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  return y + '-' + m;
}

function historyBackfillPayload(dryRun, overwriteDaily) {
  const labels = operatorText();
  const periods = HISTORY_BACKFILL_SELECTED_PERIODS.slice();
  if (!periods.length) throw new Error(labels.choosePeriodFirst);
  const runMode = document.getElementById('historyBackfillRunMode')?.value || 'now';
  const scheduledAt = document.getElementById('historyBackfillScheduledAt')?.value || '';
  const skipReady = Boolean(document.getElementById('historyBackfillSkipReady')?.checked);
  if (!dryRun && runMode === 'scheduled' && !scheduledAt) throw new Error(labels.chooseScheduleTime);
  const starts = periods.map(p => p.start).sort();
  const ends = periods.map(p => p.end).sort();
  return {
    start: starts[0],
    end: ends[ends.length - 1],
    grain: 'selected',
    periods,
    includeSummaries: Boolean(document.getElementById('historyBackfillSummaries')?.checked),
    skipReady,
    overwriteDaily: !dryRun && Boolean(overwriteDaily),
    scheduledAt: (!dryRun && runMode === 'scheduled') ? scheduledAt : null,
    dryRun: Boolean(dryRun)
  };
}

function historyBackfillPlanKey(payload) {
  return JSON.stringify(Object.assign({}, payload, {dryRun: true, scheduledAt: null, overwriteDaily: false}));
}

function historyBackfillInvalidatePreview() {
  HISTORY_BACKFILL_LAST_PLAN = null;
  HISTORY_BACKFILL_LAST_PLAN_KEY = '';
  HISTORY_BACKFILL_LAST_PLAN_PAYLOAD = null;
  const preview = document.getElementById('historyBackfillPreview');
  const status = document.getElementById('historyBackfillStatus');
  const btn = document.getElementById('historyBackfillRunBtn');
  if (preview) preview.innerHTML = '';
  if (status) status.textContent = operatorText().estimateFirst;
  if (btn) {
    btn.disabled = true;
    btn.title = operatorText().dryRunRequiredBeforeQueue;
  }
}

function renderHistoryBackfillPlan(plan) {
  const labels = operatorText();
  const items = Array.isArray(plan.pendingItems) ? plan.pendingItems : [];
  const rows = items.slice(0, 120).map(item =>
    '<tr><td>' + escapeHtml(historyBackfillPendingType(item.kind)) + '</td><td>' + escapeHtml(historyBackfillPendingLabel(item)) + '</td><td>' + escapeHtml(item.llmCalls || 0) + '</td></tr>'
  ).join('');
  const empty = rows || '<tr><td colspan="3">' + escapeHtml(labels.noMissingItems) + '</td></tr>';
  const overflow = items.length > 120 ? '<div class="settings-note">' + escapeHtml(labels.previewFirstItems) + '</div>' : '';
  return '<div class="history-backfill-plan">' +
    '<div class="settings-runtime-line"><b>' + escapeHtml(labels.pendingItems) + '</b> ' + escapeHtml(plan.pendingItemCount || items.length || 0) + ' · <b>' + escapeHtml(labels.missingDiaries) + '</b> ' + escapeHtml(plan.pendingDiaryDays || 0) + ' · <b>' + escapeHtml(labels.existingDiaries) + '</b> ' + escapeHtml(plan.existingDiaryDays || 0) + ' · <b>' + escapeHtml(labels.missingSummaries) + '</b> ' + escapeHtml(plan.pendingSummaryReports || 0) + ' · <b>' + escapeHtml(labels.maxLlmCalls) + '</b> ' + escapeHtml(plan.llmCallCount || 0) + '</div>' +
    '<div class="settings-note">' + escapeHtml((plan.warnings || []).join(' ')) + '</div>' +
    '<div class="history-backfill-periods"><table><thead><tr><th>' + escapeHtml(labels.type) + '</th><th>' + escapeHtml(labels.pendingItem) + '</th><th>' + escapeHtml(labels.maxLlmCalls) + '</th></tr></thead><tbody>' + empty + '</tbody></table></div>' +
    overflow +
    '</div>';
}

function historyBackfillPendingLabel(item) {
  const parts = [item.label || ''];
  if (Array.isArray(item.missingLabels) && item.missingLabels.length) {
    parts.push(item.missingLabels.join(', '));
  }
  if (item.overwrite) parts.push('overwrite');
  return parts.filter(Boolean).join(' · ');
}

function historyBackfillPendingType(kind) {
  const labels = operatorText();
  if (kind === 'diary') return labels.diary;
  if (kind === 'month-summary') return labels.monthlyReport;
  if (kind === 'week-summary') return labels.weeklyReport;
  return labels.other;
}

async function previewHistoryBackfill() {
  const labels = operatorText();
  const status = document.getElementById('historyBackfillStatus');
  const preview = document.getElementById('historyBackfillPreview');
  if (status) status.textContent = labels.calculatingPlan;
  if (preview) preview.innerHTML = '';
  try {
    const payload = historyBackfillPayload(true);
    const planKey = historyBackfillPlanKey(payload);
    const res = await fetch('/api/foundation/history-backfill', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    HISTORY_BACKFILL_LAST_PLAN = data;
    HISTORY_BACKFILL_LAST_PLAN_KEY = planKey;
    HISTORY_BACKFILL_LAST_PLAN_PAYLOAD = payload;
    if (status) status.textContent = labels.dryRunReady;
    if (preview) preview.innerHTML = renderHistoryBackfillPlan(data);
    const runBtn = document.getElementById('historyBackfillRunBtn');
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.title = '';
    }
  } catch (e) {
    if (status) status.textContent = labels.planFailed + e.message;
  }
}

async function startHistoryBackfill() {
  const labels = operatorText();
  const status = document.getElementById('historyBackfillStatus');
  const btn = document.getElementById('historyBackfillRunBtn');
  if (btn) btn.disabled = true;
  if (status) status.textContent = labels.submittingBackgroundTask;
  try {
    const skipReady = Boolean(document.getElementById('historyBackfillSkipReady')?.checked);
    const planPayload = historyBackfillPayload(true);
    const planKey = historyBackfillPlanKey(planPayload);
    if (!HISTORY_BACKFILL_LAST_PLAN) {
      if (status) status.textContent = labels.dryRunRequiredBeforeQueue;
      return;
    }
    if (HISTORY_BACKFILL_LAST_PLAN_KEY !== planKey) {
      HISTORY_BACKFILL_LAST_PLAN = null;
      HISTORY_BACKFILL_LAST_PLAN_KEY = '';
      if (status) status.textContent = labels.dryRunStale;
      return;
    }
    let overwriteDaily = false;
    const overwriteItems = Array.isArray(HISTORY_BACKFILL_LAST_PLAN?.overwriteItems) ? HISTORY_BACKFILL_LAST_PLAN.overwriteItems : [];
    if (overwriteItems.length) {
      const names = overwriteItems.slice(0, 20).map(historyBackfillPendingLabel).join('、');
      const suffix = overwriteItems.length > 20 ? `… +${overwriteItems.length - 20}` : '';
      overwriteDaily = window.confirm(labels.overwriteConfirm(names + suffix));
      if (!overwriteDaily) {
        if (status) status.textContent = labels.overwriteCancelled;
        return;
      }
    }
    const scheduledAt = document.getElementById('historyBackfillScheduledAt')?.value || '';
    const runMode = document.getElementById('historyBackfillRunMode')?.value || 'now';
    if (runMode === 'scheduled' && !scheduledAt) throw new Error(labels.chooseScheduleTime);
    const payload = Object.assign({}, HISTORY_BACKFILL_LAST_PLAN_PAYLOAD || planPayload, {
      dryRun: false,
      overwriteDaily: Boolean(overwriteDaily),
      scheduledAt: runMode === 'scheduled' ? scheduledAt : null
    });
    const res = await fetch('/api/foundation/history-backfill', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    const started = data.status === 'scheduled' ? labels.scheduled : labels.startedRun;
    if (status) status.textContent = started + ' Run #' + data.runId + labels.runDetailsHint;
    const preview = document.getElementById('historyBackfillPreview');
    if (preview) {
      preview.innerHTML = '<div class="history-backfill-plan">' +
        '<div class="settings-runtime-line"><b>' + escapeHtml(started) + ' Run #' + escapeHtml(data.runId) + '</b> · ' + escapeHtml(labels.period) + ' ' + escapeHtml(data.periodCount || 0) + ' · ' + escapeHtml(labels.dailyPipeline) + ' ' + escapeHtml(data.dailyPipelineDays || 0) + ' ' + escapeHtml(labels.days) + ' · ' + escapeHtml(labels.maxLlmCalls) + ' ' + escapeHtml(data.llmCallCount || 0) + '</div>' +
        (data.overwriteDaily ? '<div class="settings-note">' + escapeHtml(labels.overwriteConfirmed) + '</div>' : '') +
        '<div class="settings-note">' + escapeHtml(labels.historyQueued) + '</div>' +
        '<button type="button" class="wr-export-btn secondary" onclick="openBackgroundTasksModal()">' + escapeHtml(labels.viewBackgroundTasks) + '</button>' +
        '</div>';
    }
    await refreshBackgroundTaskButton();
  } catch (e) {
    if (status) status.textContent = labels.submitFailed + e.message;
  } finally {
    if (btn) {
      const readyToQueue = Boolean(HISTORY_BACKFILL_LAST_PLAN && HISTORY_BACKFILL_LAST_PLAN_KEY);
      btn.disabled = !readyToQueue;
      btn.title = readyToQueue ? '' : labels.dryRunRequiredBeforeQueue;
    }
  }
}

function openHistoryBackfillModal() {
  const labels = operatorText();
  const selected = renderHistoryBackfillSelected();
  replaceModalContent(labels.historyBackfillTitle, `
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.historyBackfillSection)}</div>
      <div class="settings-note">${escapeHtml(labels.historyBackfillNote)}</div>
      <div class="settings-row"><label>${escapeHtml(labels.selectedPeriods)}</label><div><div id="historyBackfillSelectedBox" class="history-backfill-selected">${selected}</div><button type="button" class="wr-export-btn secondary" onclick="openHistoryBackfillPeriodPicker()">${escapeHtml(labels.choosePeriod)}</button></div></div>
      <div class="settings-row"><label>${escapeHtml(labels.runMode)}</label><select id="historyBackfillRunMode" onchange="toggleHistoryBackfillSchedule(); historyBackfillInvalidatePreview()"><option value="now">${escapeHtml(labels.runNow)}</option><option value="scheduled">${escapeHtml(labels.runScheduled)}</option></select></div>
      <div class="settings-row" id="historyBackfillScheduleRow" style="display:none"><label>${escapeHtml(labels.scheduledTime)}</label><input id="historyBackfillScheduledAt" type="datetime-local" onchange="historyBackfillInvalidatePreview()"></div>
      <div class="settings-checks">
        <label class="settings-check"><input id="historyBackfillSummaries" type="checkbox" onchange="historyBackfillInvalidatePreview()"> ${escapeHtml(labels.generateSummaries)}</label>
        <label class="settings-check"><input id="historyBackfillSkipReady" type="checkbox" checked onchange="historyBackfillInvalidatePreview()"> ${escapeHtml(labels.skipReady)}</label>
      </div>
      <div class="settings-note">${escapeHtml(labels.overwriteNote)}</div>
      <div class="settings-actions">
        <span class="settings-status" id="historyBackfillStatus">${escapeHtml(labels.estimateFirst)}</span>
        <button type="button" class="wr-export-btn secondary" onclick="previewHistoryBackfill()">${escapeHtml(labels.dryRunEstimate)}</button>
        <button type="button" class="wr-export-btn" id="historyBackfillRunBtn" onclick="startHistoryBackfill()" disabled title="${escapeHtml(labels.dryRunRequiredBeforeQueue)}">${escapeHtml(labels.queueGeneration)}</button>
      </div>
      <div id="historyBackfillPreview"></div>
    </div>
  `);
}

function historyBackfillPeriodLabel(period) {
  if (period.kind === 'month') return period.label || period.start.slice(0, 7);
  const end = period.end || '';
  const week = String(period.label || '').match(/W\d{1,2}/);
  return (end.slice(5, 7) || '--') + '-' + (week ? week[0] : period.label || 'week');
}

function renderHistoryBackfillSelected() {
  if (!HISTORY_BACKFILL_SELECTED_PERIODS.length) return '<span class="settings-note">' + escapeHtml(operatorText().noSelectedPeriods) + '</span>';
  return HISTORY_BACKFILL_SELECTED_PERIODS
    .map(period => '<span class="settings-runtime-chip ok">' + escapeHtml(historyBackfillPeriodLabel(period)) + '</span>')
    .join('');
}

function historyBackfillPickerPeriods() {
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth() - 5, 1);
  const months = [];
  for (let i = 0; i < 6; i++) {
    const first = new Date(start.getFullYear(), start.getMonth() + i, 1);
    const endDay = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
    const ym = historyBackfillMonthValue(first);
    months.push({kind:'month', label:ym, start:ym + '-01', end:ym + '-' + String(endDay).padStart(2, '0')});
  }
  const weeks = [];
  const cursor = new Date(start);
  cursor.setDate(cursor.getDate() + ((7 - cursor.getDay()) % 7));
  while (cursor <= today) {
    const end = new Date(cursor);
    const begin = new Date(end);
    begin.setDate(end.getDate() - 6);
    const iso = isoWeekLabel(end);
    weeks.push({kind:'week', label:iso, start:dateInputValue(begin), end:dateInputValue(end)});
    cursor.setDate(cursor.getDate() + 7);
  }
  return {months, weeks};
}

function dateInputValue(date) {
  return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
}

function isoWeekLabel(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const dayNum = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const weekNo = Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
  return d.getUTCFullYear() + '-W' + String(weekNo).padStart(2, '0');
}

function openHistoryBackfillPeriodPicker() {
  const labels = operatorText();
  const data = historyBackfillPickerPeriods();
  HISTORY_BACKFILL_PICKER_PERIODS = [];
  const selectedKeys = new Set(HISTORY_BACKFILL_SELECTED_PERIODS.map(p => p.kind + ':' + p.start + ':' + p.end));
  const grouped = data.months.map(month => {
    const monthKey = month.kind + ':' + month.start + ':' + month.end;
    const monthIndex = HISTORY_BACKFILL_PICKER_PERIODS.push(month) - 1;
    const monthChecked = selectedKeys.has(monthKey) ? 'checked' : '';
    const weeks = data.weeks.filter(week => week.start.slice(0, 7) === month.label);
    const weekRows = weeks.map(week => {
      const weekKey = week.kind + ':' + week.start + ':' + week.end;
      const weekIndex = HISTORY_BACKFILL_PICKER_PERIODS.push(week) - 1;
      const checked = selectedKeys.has(weekKey) ? 'checked' : '';
      return '<label class="history-backfill-week"><input type="checkbox" data-history-period-index="' + weekIndex + '" ' + checked + '> <span>' + escapeHtml(historyBackfillPeriodLabel(week)) + '</span><small>' + escapeHtml(week.start + ' ~ ' + week.end) + '</small></label>';
    }).join('');
    return '<div class="history-backfill-month-group">' +
      '<label class="settings-check history-backfill-month"><input type="checkbox" data-history-period-index="' + monthIndex + '" ' + monthChecked + '> <b>' + escapeHtml(month.label) + '</b><span>' + escapeHtml(month.start + ' ~ ' + month.end) + '</span></label>' +
      '<div class="history-backfill-weeks">' + weekRows + '</div>' +
      '</div>';
  }).join('');
  replaceModalContent(labels.periodPickerTitle, `
    <div class="settings-section">
      <div class="settings-note">${escapeHtml(labels.periodPickerNote)}</div>
      <div class="history-backfill-picker">${grouped}</div>
      <div class="settings-actions">
        <button type="button" class="wr-export-btn secondary" onclick="openHistoryBackfillModal()">${escapeHtml(labels.cancel)}</button>
        <button type="button" class="wr-export-btn" onclick="confirmHistoryBackfillPeriods()">${escapeHtml(labels.confirmSelection)}</button>
      </div>
    </div>
  `);
}

function confirmHistoryBackfillPeriods() {
  const selected = [];
  document.querySelectorAll('[data-history-period-index]:checked').forEach(input => {
    const period = HISTORY_BACKFILL_PICKER_PERIODS[Number(input.dataset.historyPeriodIndex)];
    if (period) selected.push(period);
  });
  selected.sort((a, b) => (a.end || '').localeCompare(b.end || '') || (a.kind || '').localeCompare(b.kind || ''));
  HISTORY_BACKFILL_SELECTED_PERIODS = selected;
  HISTORY_BACKFILL_LAST_PLAN = null;
  HISTORY_BACKFILL_LAST_PLAN_KEY = '';
  HISTORY_BACKFILL_LAST_PLAN_PAYLOAD = null;
  openHistoryBackfillModal();
}

function toggleHistoryBackfillSchedule() {
  const row = document.getElementById('historyBackfillScheduleRow');
  const mode = document.getElementById('historyBackfillRunMode')?.value || 'now';
  if (row) row.style.display = mode === 'scheduled' ? '' : 'none';
}

function replaceModalContent(title, content) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = content;
  document.getElementById('modal').classList.add('active');
  document.body.style.overflow = 'hidden';
  modalHistory = [{ title, content }];
}

async function refreshMsgbox() {
  try {
    const res = await fetch('/api/msgbox?limit=20');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    MSGBOX_STATE = await res.json();
    renderMsgboxButton(MSGBOX_STATE);
  } catch (e) {
    renderMsgboxButton({count: 0, attentionCount: 0, error: e.message});
  }
}

function renderMsgboxButton(state) {
  const labels = operatorText();
  const button = document.getElementById('msgboxButton');
  const count = document.getElementById('msgboxCount');
  if (!button || !count) return;
  const attention = Number(state.attentionCount || 0);
  const total = Number(state.count || 0);
  button.classList.toggle('has-attention', attention > 0);
  count.textContent = String(total);
  button.title = attention > 0 ? labels.attentionItems(attention) : labels.messagesTitle;
}

function msgboxSeverityLabel(severity) {
  const labels = operatorText();
  if (severity === 'error') return labels.severityError;
  if (severity === 'warn') return labels.severityWarn;
  return labels.severityInfo;
}

function renderMsgboxItem(item) {
  const action = item.action || {};
  const actionButton = item.actionLabel
    ? '<button class="wr-export-btn" onclick="handleMsgboxActionPayload(\'' + encodeURIComponent(JSON.stringify(action)) + '\')">' + escapeHtml(item.actionLabel) + '</button>'
    : '';
  const readButton = '<button class="wr-export-btn secondary" onclick="markMsgboxRead(\'' + escapeHtml(item.id || '') + '\')">' + escapeHtml(operatorText().read) + '</button>';
  const details = item.details ? '<pre class="settings-json-preview">' + escapeHtml(JSON.stringify(item.details, null, 2)) + '</pre>' : '';
  return '<div class="msgbox-item ' + escapeHtml(item.severity || 'info') + '">' +
    '<div class="msgbox-item-title"><span>' + escapeHtml(item.title || item.type || operatorText().message) + '</span><span class="fo-status fo-status-' + escapeHtml(item.severity || 'info') + '">' + msgboxSeverityLabel(item.severity) + '</span></div>' +
    '<div class="msgbox-item-summary">' + escapeHtml(item.summary || '') + '</div>' +
    '<div class="msgbox-item-meta">' + escapeHtml((item.createdAt || '').replace('T', ' ').slice(0, 19)) + '</div>' +
    '<div class="msgbox-actions">' + (actionButton || '') + readButton + '</div>' +
    details +
    '</div>';
}

function handleMsgboxActionPayload(encodedAction) {
  try {
    handleMsgboxAction(JSON.parse(decodeURIComponent(encodedAction || '%7B%7D')));
  } catch (e) {
    const body = document.getElementById('modal-body');
    if (body) body.innerHTML = '<div class="fo-empty">' + escapeHtml(operatorText().openFailed + (e.message || e)) + '</div>';
  }
}

function openDashboardPage(pageId) {
  const page = String(pageId || 'overview').replace(/^page-/, '');
  if (page === 'tasks' || page === 'task-board') {
    window.location.assign(window.ACTANARA_STATIC_DEMO ? 'tasks.html' : '/tasks');
    return;
  }
  closeModal();
  showPage(page);
}

function handleMsgboxAction(action) {
  try {
    if (!action || !action.kind) return;
    if (action.kind === 'openPage') {
      openDashboardPage(action.page || 'overview');
      return;
    }
    if (action.kind === 'openUrl' && action.url) {
      window.location.assign(action.url);
      return;
    }
    if (action.kind === 'apiPost' && action.url) {
      handleMsgboxApiPost(action);
      return;
    }
  } catch (e) {
    document.getElementById('modal-body').innerHTML = '<div class="fo-empty">' + escapeHtml(operatorText().openFailed + (e.message || e)) + '</div>';
  }
}

async function handleMsgboxApiPost(action) {
  const body = document.getElementById('modal-body');
  try {
    const res = await fetch(action.url, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (action.refreshBackgroundTasks) await refreshBackgroundTaskButton();
    await refreshMsgbox();
    if (body) {
      body.innerHTML = '<div class="fo-empty">' + escapeHtml(action.successMessage || operatorText().actionSubmitted) + (data.runId ? ' · Run #' + escapeHtml(data.runId) : '') + '</div>';
    }
  } catch (e) {
    if (body) body.innerHTML = '<div class="fo-job-error">' + escapeHtml(operatorText().actionFailed + (e.message || e)) + '</div>';
  }
}

async function markMsgboxRead(messageId) {
  if (!messageId) return;
  const res = await fetch('/api/msgbox/' + encodeURIComponent(messageId) + '/read', { method: 'POST' });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  await refreshMsgbox();
  const items = Array.isArray(MSGBOX_STATE.items) ? MSGBOX_STATE.items : [];
  document.getElementById('modal-body').innerHTML = items.length
    ? '<div class="msgbox-list">' + items.map(renderMsgboxItem).join('') + '</div>'
    : '<div class="fo-empty">' + escapeHtml(operatorText().noMessages) + '</div>';
}

async function openMsgboxModal() {
  const labels = operatorText();
  openModal(labels.messagesTitle, '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingMessages) + '</span></div>');
  await refreshMsgbox();
  const items = Array.isArray(MSGBOX_STATE.items) ? MSGBOX_STATE.items : [];
  const body = items.length
    ? '<div class="msgbox-list">' + items.map(renderMsgboxItem).join('') + '</div>'
    : '<div class="fo-empty">' + escapeHtml(labels.noMessages) + '</div>';
  document.getElementById('modal-body').innerHTML = body;
}

// ─── 动态加载周报（图表化） ────────────────────────────
const WR_CHARTS = {};  // cache chart instances per page
const WR_AGENT_COLORS = {
  'openclaw': '#533afd', 'gemini-cli': '#2563eb', 'claude-code': '#dc2626',
  'hermes': '#0891b2', 'cron': '#6b7280', 'coder': '#8b5cf6',
  'main': '#059669', 'unknown': '#94a3b8',
};
function wrAgentColor(n) { return WR_AGENT_COLORS[n] || '#94a3b8'; }
function wrFormatTokens(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function wrDisplayWeekId(wk) {
  const m = String(wk || '').match(/W\d{1,2}$/i);
  return m ? m[0].toUpperCase() : String(wk || '');
}

/** Compute the Monday (YYYY-MM-DD) of the given ISO week identifier like '2026-W18' */
function wrMondayOfWeek(wk) {
  const raw = String(wk || '');
  const shortWeek = raw.match(/^W(\d{1,2})$/i);
  const normalized = shortWeek ? `${new Date().getFullYear()}-${raw}` : raw;
  const m = normalized.match(/^(\d{4})-W(\d{1,2})$/i);
  if (!m) return null;
  const year = +m[1], week = +m[2];
  const jan4 = new Date(year, 0, 4);
  const dow = jan4.getDay();
  const isoDow = dow === 0 ? 7 : dow;
  const daysToMon = 1 - isoDow + (week - 1) * 7;
  const monday = new Date(year, 0, 4 + daysToMon);
  // Use local date components to avoid UTC timezone offset (toISOString uses UTC)
  const y = monday.getFullYear();
  const mo = String(monday.getMonth() + 1).padStart(2, '0');
  const da = String(monday.getDate()).padStart(2, '0');
  return y + '-' + mo + '-' + da;
}

function wrDestroyCharts(prefix) {
  if (WR_CHARTS[prefix]) {
    for (const key of Object.keys(WR_CHARTS[prefix])) {
      WR_CHARTS[prefix][key].destroy();
    }
    delete WR_CHARTS[prefix];
  }
}

async function loadReport(reportId) {
  await ensureDashboardLanguageProfile();
  const labels = dashboardText();
  const pageId = 'page-report-' + reportId;
  // 切换页面
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  event && event.currentTarget && event.currentTarget.classList.add('active');

  let page = document.getElementById(pageId);
  if (page) {
    page.classList.add('active');
    location.hash = pageId;
    return;
  }
  // Create page
  page = document.createElement('div');
  page.id = pageId;
  page.className = 'page';
  const prefix = 'wr_' + reportId.replace(/[^a-zA-Z0-9]/g, '_');
  page.innerHTML = `
    <div class="page-header" id="${prefix}_header">
      <div class="page-title">📈 ${labels.weekLabel(reportId)}</div>
      <div class="page-subtitle" id="${prefix}_sub">${escapeHtml(labels.loadingEllipsis)}</div>
      <div class="wr-summary-quote" id="${prefix}_summaryQuote" style="display:none"></div>
      <button class="wr-export-btn" onclick="window.print()" title="${escapeHtml(labels.exportPrintTitle)}">🖨️ ${escapeHtml(labels.exportPrint)}</button>
      <button class="wr-export-btn" id="${prefix}_refreshAssets" onclick="refreshWeeklyAssets('${reportId}', '${prefix}')" title="${escapeHtml(labels.refreshAssetsTitle)}">${escapeHtml(labels.refreshAssets)}</button>
    </div>
    <div class="wr-loading" id="${prefix}_loading"><div class="wr-spinner"></div><span>${escapeHtml(labels.loadingWeekly)}</span></div>
    <div class="wr-refresh-notice" id="${prefix}_refreshNotice" style="display:none;"></div>
    <div id="${prefix}_content" style="display:none;">
      <div class="wr-section wr-narrative-section">
        <div class="wr-section-header"><div class="wr-section-title"><span class="emoji">📝</span> ${escapeHtml(labels.weeklySummary)}</div><button class="wr-narrative-btn" id="${prefix}_refreshSummary" onclick="refreshWeeklySummary('${reportId}', '${prefix}')" title="${escapeHtml(labels.generateWeeklySummaryTitle)}">${escapeHtml(labels.generateSummary)}</button></div>
        <div class="wr-card full-width wr-narrative-card" id="${prefix}_periodSummary"></div>
      </div>
      <div class="wr-section"><div class="wr-section-header"><div class="wr-section-title"><span class="emoji">📈</span> ${escapeHtml(labels.overview)}</div></div><div class="wr-kpi-grid" id="${prefix}_kpi"></div></div>
      <div class="wr-section"><div class="wr-section-header"><div class="wr-section-title"><span class="emoji">⏱️</span> ${escapeHtml(labels.timeInvestment)}</div></div><div class="wr-card full-width"><div class="wr-heatmap-container" id="${prefix}_work"></div></div></div>
      <div class="wr-section wr-chart-pair">
        <div class="wr-chart-panel"><div class="wr-section-header"><div class="wr-section-title"><span class="emoji">⚡</span> ${escapeHtml(labels.usageTrend)}</div></div><div class="wr-card full-width"><div class="wr-chart-box mr-composite-chart"><canvas id="${prefix}_trendChart"></canvas></div></div></div>
        <div class="wr-chart-panel"><div class="wr-section-header"><div class="wr-section-title"><span class="emoji">🤖</span> ${escapeHtml(labels.modelRanking)}</div></div><div class="wr-card full-width"><div class="wr-chart-box mr-model-chart"><canvas id="${prefix}_modelChart"></canvas></div></div></div>
      </div>
      <div class="wr-section"><div class="wr-section-header"><div class="wr-section-title"><span class="emoji">👥</span> ${escapeHtml(labels.agentWorkspaceRanking)}</div></div><div class="wr-card-grid mr-usage-grid">
        <div class="wr-card"><div class="wr-card-title"><span class="emoji">⚡</span> ${escapeHtml(labels.tokenRank)}</div><div id="${prefix}_usageTokens"></div></div>
        <div class="wr-card"><div class="wr-card-title"><span class="emoji">💬</span> ${escapeHtml(labels.messageActivity)}</div><div id="${prefix}_usageMessages"></div></div>
        <div class="wr-card"><div class="wr-card-title"><span class="emoji">📅</span> ${escapeHtml(labels.activeDays)}</div><div id="${prefix}_usageDays"></div></div>
        <div class="wr-card"><div class="wr-card-title"><span class="emoji">📊</span> ${escapeHtml(labels.workloadComparison)}</div><div id="${prefix}_workload"></div></div>
        <div class="wr-card"><div class="wr-card-title"><span class="emoji">⏰</span> ${escapeHtml(labels.scheduledJobs)}</div><div id="${prefix}_cron"></div></div>
        <div class="wr-card"><div class="wr-card-title"><span class="emoji">🧠</span> ${escapeHtml(labels.ragChange)}</div><div id="${prefix}_knowledge"></div></div>
      </div><div class="mr-usage-note" id="${prefix}_usageNote"></div></div>
      <div class="wr-section"><div class="wr-card-grid"><div class="wr-card full-width"><div class="wr-card-title"><span class="emoji">🏷️</span> ${escapeHtml(labels.highFrequencyTopics)}</div><div class="wr-topic-source">${escapeHtml(labels.topicSource)}</div><div class="wr-topic-tags" id="${prefix}_topics"></div></div></div></div>
      <div class="wr-section"><div class="wr-card-grid"><div class="wr-card full-width"><div class="wr-card-title"><span class="emoji">🎯</span> ${escapeHtml(labels.tasksOutcomes)}</div><div class="wr-summary-list" id="${prefix}_summary"></div></div></div></div>
      <div class="wr-section"><div class="wr-card-grid"><div class="wr-card full-width"><div class="wr-card-title"><span class="emoji">💡</span> ${escapeHtml(labels.lessons)} <span id="${prefix}_lessonCount" style="font-size:12px;color:var(--slate);font-weight:400;"></span></div><div class="wr-lesson-filter" id="${prefix}_lessonFilter"></div><div class="wr-lessons-list" id="${prefix}_lessons"></div></div></div></div>
      <div class="wr-section" id="${prefix}_summaryDetailsSection" style="display:none"><div class="wr-section-header"><div class="wr-section-title"><span class="emoji">📋</span> ${escapeHtml(labels.summaryDetails)}</div></div><div class="wr-card full-width wr-summary-details" id="${prefix}_summaryDetails"></div></div>
    </div>`;
  document.getElementById('diary-pages').appendChild(page);
  page.classList.add('active');
  location.hash = pageId;

  // Calculate start date from week identifier
  const startDate = wrMondayOfWeek(reportId);
  const url = startDate
    ? `/api/weekly-report?days=7&start=${startDate}&include_assets=true`
    : `/api/weekly-report?days=7&include_assets=true`;

  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    document.getElementById(prefix + '_loading').style.display = 'none';
    document.getElementById(prefix + '_content').style.display = 'block';
    document.getElementById(prefix + '_sub').textContent = (data.period || reportId) + ' · ' + (data.days || 0) + ' ' + labels.dayUnit;
    wrRenderRefreshNotice(prefix, data.dataFreshness, startDate, 7, async () => {
      const page = document.getElementById(pageId);
      if (page) page.remove();
      await loadReport(reportId);
    });
    wrDestroyCharts(prefix);
    WR_CHARTS[prefix] = {};
    wrRenderKPI(prefix, data.kpi);
    mrRenderCompositeTrend(data.dailyTokenSeries, prefix);
    mrRenderModels(data.models || [], prefix);
    mrRenderUsageLists(data.workspaceUsage, data.agentActivity, 7, prefix);
    wrRenderWorkloadComparison(prefix, data.workloadComparison);
    wrRenderCronStats(prefix, data.cronStats);
    mrRenderKnowledge(data.knowledgePeriod, prefix, labels.weeklyKnowledgePeriod);
    wrRenderTopics(prefix, data.highFrequencyTopics || data.topTopics);
    wrRenderPeriodSummary(prefix, data.periodSummary, data.dataFreshness && data.dataFreshness.periodSummary);
    wrRenderSummaryTopics(prefix, data.summaryTopics);
    wrRenderAgentWork(prefix, data.agentWork, data.hourlyHeatmap);
    wrRenderLessons(prefix, data.lessons);
  } catch (e) {
    document.getElementById(prefix + '_loading').innerHTML = '<span style="color:var(--ruby)">❌ ' + escapeHtml(labels.loadFailed) + escapeHtml(e.message) + '</span>';
  }
}

// ─── 月报加载（复用周报 API，days=30） ────────────────────
const MR_PREFIX = 'mr';
const MR_LOADED = {};  // track loaded months
let MR_CURRENT_MONTH = null;
let MR_REQUEST_TOKEN = 0;

function updateMonthlyReportLabels() {
  applyStaticDashboardText(ACTANARA_PIPELINE_LANGUAGE_PROFILE);
}

function loadMonthlyReportById(mk) {
  const labels = dashboardText();
  updateMonthlyReportLabels();
  // mk = '2026-05'
  if (MR_LOADED[mk] && MR_CURRENT_MONTH === mk) {
    showPage('monthly-overview');
    return;
  }
  // Reset UI to loading state
  const loading = document.getElementById(MR_PREFIX + '_loading');
  const content = document.getElementById(MR_PREFIX + '_content');
  if (loading) loading.style.display = '';
  if (content) content.style.display = 'none';
  if (loading) loading.innerHTML = '<div class="wr-spinner"></div><span>' + escapeHtml(labels.loadingMonthly) + '</span>';
  const notice = document.getElementById(MR_PREFIX + '_refreshNotice');
  if (notice) notice.style.display = 'none';
  wrDestroyCharts(MR_PREFIX);
  WR_CHARTS[MR_PREFIX] = {};
  showPage('monthly-overview');
  loadMonthlyReport(mk);
}

async function loadMonthlyReport(mk) {
  await ensureDashboardLanguageProfile();
  const labels = dashboardText();
  updateMonthlyReportLabels();
  // mk = '2026-05' or undefined (current month)
  const loading = document.getElementById(MR_PREFIX + '_loading');
  const content = document.getElementById(MR_PREFIX + '_content');
  const sub = document.getElementById(MR_PREFIX + '_sub');
  if (!loading) return;

  if (!mk) {
    const now = new Date();
    mk = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0');
  }
  MR_CURRENT_MONTH = mk;
  const requestToken = ++MR_REQUEST_TOKEN;
  const parts = mk.split('-');
  const year = +parts[0];
  const month = +parts[1] - 1; // 0-based
  const startDate = mk + '-01';
  const monthLabel = labels.monthLabel(year, month + 1);
  const daysInMonth = new Date(year, month + 1, 0).getDate();

  sub.textContent = monthLabel + ' · ' + labels.monthlySummary;

  try {
    const url = '/api/weekly-report?days=' + daysInMonth + '&start=' + startDate + '&include_assets=true';
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (requestToken !== MR_REQUEST_TOKEN || MR_CURRENT_MONTH !== mk) return;

    MR_LOADED[mk] = true;
    loading.style.display = 'none';
    content.style.display = 'block';
    sub.textContent = monthLabel + ' · ' + (data.days || 0) + ' ' + labels.dayDataUnit;
    wrRenderRefreshNotice(MR_PREFIX, data.dataFreshness, startDate, daysInMonth, async () => {
      MR_LOADED[mk] = false;
      await loadMonthlyReportById(mk);
    });

    wrDestroyCharts(MR_PREFIX);
    WR_CHARTS[MR_PREFIX] = {};
    wrRenderKPI(MR_PREFIX, data.kpi);
    mrRenderPulse(data.kpi || {}, data.dailyTokenSeries || [], daysInMonth);
    mrRenderCompositeTrend(data.dailyTokenSeries);
    mrRenderModels(data.models || []);
    mrRenderUsageLists(data.workspaceUsage, data.agentActivity, daysInMonth);
    wrRenderWorkloadComparison(MR_PREFIX, data.workloadComparison);
    wrRenderCronStats(MR_PREFIX, data.cronStats);
    mrRenderKnowledge(data.knowledgePeriod);
    wrRenderTopics(MR_PREFIX, data.highFrequencyTopics || data.topTopics);
    wrRenderPeriodSummary(MR_PREFIX, data.periodSummary, data.dataFreshness && data.dataFreshness.periodSummary);
    wrRenderSummaryTopics(MR_PREFIX, data.summaryTopics);
    mrRenderHeatmap(data.assetHourlyHeatmap || data.hourlyHeatmap);
    wrRenderLessons(MR_PREFIX, data.lessons);
  } catch (e) {
    if (requestToken !== MR_REQUEST_TOKEN || MR_CURRENT_MONTH !== mk) return;
    loading.innerHTML = '<span style="color:var(--ruby)">❌ ' + escapeHtml(labels.loadFailed) + escapeHtml(e.message) + '</span>';
  }
}

function foundationFreshnessSuffix(meta) {
  if (!meta) return '';
  const labels = dashboardText();
  if (meta.source === 'foundation') {
    const generated = (meta.generatedAt || '').replace('T', ' ').slice(0, 16);
    return generated ? ' · ' + labels.snapshot + ' ' + generated : ' · ' + labels.foundationSnapshot;
  }
  if (meta.source === 'snapshot-missing') return ' · ' + labels.snapshotMissing;
  return '';
}

function periodPageFreshnessSuffix(meta) {
  if (!meta) return '';
  const labels = dashboardText();
  if (meta.source === 'foundation') {
    const generated = (meta.generatedAt || '').replace('T', ' ').slice(0, 16);
    return generated ? ' · ' + labels.pageSnapshot + ' ' + generated : ' · ' + labels.foundationPage;
  }
  if (meta.source === 'snapshot-missing' && meta.status === 'projection_missing') return ' · ' + labels.pageSnapshotMissing;
  return '';
}

function periodSummaryFreshnessSuffix(meta) {
  if (!meta) return '';
  const labels = dashboardText();
  if (meta.source === 'foundation') {
    const generated = (meta.generatedAt || '').replace('T', ' ').slice(0, 16);
    return generated ? ' · ' + labels.summarySnapshot + ' ' + generated : ' · ' + labels.summarySnapshot;
  }
  if (meta.source === 'snapshot-missing') return ' · ' + labels.summarySnapshotMissing;
  return '';
}

function reportFreshnessSuffix(meta) {
  if (!meta) return '';
  return foundationFreshnessSuffix(meta.periodAssets) + periodPageFreshnessSuffix(meta.periodPage) + periodSummaryFreshnessSuffix(meta.periodSummary);
}

function reportNeedsAssetRefresh(meta) {
  if (!meta) return false;
  return (meta.periodAssets && meta.periodAssets.source === 'snapshot-missing')
    || (meta.periodPage && meta.periodPage.status === 'projection_missing');
}

function wrRenderRefreshNotice(prefix, freshness, startDate, days, reload) {
  const el = document.getElementById(prefix + '_refreshNotice');
  if (!el) return;
  const labels = dashboardText();
  if (!reportNeedsAssetRefresh(freshness) || !startDate) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  const buttonId = prefix + '_rebuildSnapshot';
  el.style.display = 'flex';
  el.innerHTML = `
    <div class="wr-refresh-copy">
      <div class="wr-refresh-title">${escapeHtml(labels.snapshotMissingTitle)}</div>
      <div class="wr-refresh-desc">${escapeHtml(labels.snapshotMissingDesc)}</div>
    </div>
    <button class="wr-export-btn wr-refresh-action" id="${buttonId}" type="button">${escapeHtml(labels.rebuildData)}</button>`;
  const btn = document.getElementById(buttonId);
  if (btn) {
    btn.onclick = () => refreshPeriodAssets(startDate, days, buttonId, reload);
  }
}

async function waitForFoundationRefresh(runId, updateStatus) {
  const labels = foundationText();
  for (let attempt = 0; attempt < 90; attempt++) {
    const res = await fetch('/api/foundation/refresh-jobs/' + runId);
    if (!res.ok) throw new Error(labels.repairStatusReadFailed + res.status);
    const job = await res.json();
    updateStatus(job.status);
    if (job.status === 'completed') return;
    if (job.status === 'failed') throw new Error(job.error_summary || labels.backgroundUpdateFailed);
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error(foundationText().repairStillRunning);
}

async function refreshPeriodAssets(startDate, days, buttonId, reload) {
  const labels = dashboardText();
  const btn = document.getElementById(buttonId);
  const original = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = labels.submitting;
  }
  try {
    const res = await fetch('/api/weekly-report/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start: startDate, days: days})
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const queued = await res.json();
    await waitForFoundationRefresh(queued.runId, status => {
      if (btn) btn.textContent = status === 'queued' ? labels.queued : labels.updating;
    });
    await reload();
  } catch (e) {
    if (btn) btn.textContent = labels.updateFailed;
    window.alert(labels.assetRefreshFailed + e.message);
    return;
  } finally {
    if (btn && btn.textContent !== labels.updateFailed) {
      btn.disabled = false;
      btn.textContent = original || labels.refreshAssets;
    } else if (btn) {
      btn.disabled = false;
    }
  }
}

async function refreshPeriodSummary(startDate, days, buttonId, reload) {
  const labels = dashboardText();
  const btn = document.getElementById(buttonId);
  const original = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = labels.submitting;
  }
  try {
    const res = await fetch('/api/weekly-report/summary/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start: startDate, days: days})
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const queued = await res.json();
    await waitForFoundationRefresh(queued.runId, status => {
      if (btn) btn.textContent = status === 'queued' ? labels.queued : labels.generating;
    });
    await reload();
  } catch (e) {
    if (btn) btn.textContent = labels.generationFailed;
    window.alert(labels.summaryRefreshFailed + e.message);
    return;
  } finally {
    if (btn && btn.textContent !== labels.generationFailed) {
      btn.disabled = false;
      btn.textContent = original || labels.generateSummary;
    } else if (btn) {
      btn.disabled = false;
    }
  }
}

function refreshWeeklyAssets(reportId, prefix) {
  const startDate = wrMondayOfWeek(reportId);
  if (!startDate) return;
  refreshPeriodAssets(startDate, 7, prefix + '_refreshAssets', async () => {
    const page = document.getElementById('page-report-' + reportId);
    if (page) page.remove();
    await loadReport(reportId);
  });
}

function refreshWeeklySummary(reportId, prefix) {
  const startDate = wrMondayOfWeek(reportId);
  if (!startDate) return;
  refreshPeriodSummary(startDate, 7, prefix + '_refreshSummary', async () => {
    const page = document.getElementById('page-report-' + reportId);
    if (page) page.remove();
    await loadReport(reportId);
  });
}

function refreshMonthlyAssets() {
  const mk = MR_CURRENT_MONTH;
  if (!mk) return;
  const parts = mk.split('-');
  const days = new Date(+parts[0], +parts[1], 0).getDate();
  refreshPeriodAssets(mk + '-01', days, 'mrRefreshAssetsBtn', async () => {
    MR_LOADED[mk] = false;
    loadMonthlyReportById(mk);
  });
}

function refreshMonthlySummary() {
  const mk = MR_CURRENT_MONTH;
  if (!mk) return;
  const parts = mk.split('-');
  const days = new Date(+parts[0], +parts[1], 0).getDate();
  refreshPeriodSummary(mk + '-01', days, 'mrRefreshSummaryBtn', async () => {
    MR_LOADED[mk] = false;
    loadMonthlyReportById(mk);
  });
}

function mrRenderCompositeTrend(series, prefix = MR_PREFIX) {
  const labelsText = dashboardText();
  const el = document.getElementById(prefix + '_trendChart');
  if (!el || !series || !series.length) return;
  const labels = series.map(item => item.displayDate || item.date);
  WR_CHARTS[prefix].trend = new Chart(el, {
    data: {
      labels,
      datasets: [
        {
          type: 'bar', label: 'Tokens', yAxisID: 'yTokens',
          data: series.map(item => item.tokens || 0),
          backgroundColor: '#533afd', borderRadius: 4, borderSkipped: false,
        },
        {
          type: 'bar', label: 'Messages', yAxisID: 'yMessages',
          data: series.map(item => item.tokens > 0 ? (item.messages || 0) : null),
          backgroundColor: 'rgba(217,119,6,0.68)', borderRadius: 4, borderSkipped: false,
        },
        {
          type: 'line', label: labelsText.cacheHitRate, yAxisID: 'yRate',
          data: series.map(item => item.tokens > 0 ? (item.cacheHitRate || 0) : null),
          borderColor: '#15be53', backgroundColor: 'rgba(21,190,83,0.10)',
          pointBackgroundColor: '#15be53', pointRadius: 3, tension: 0.3, spanGaps: false,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, padding: 16, font: { size: 11 } } },
        tooltip: { callbacks: {
          label: context => context.dataset.yAxisID === 'yTokens'
            ? 'Tokens: ' + wrFormatTokens(context.raw || 0)
            : context.dataset.yAxisID === 'yRate'
              ? labelsText.cacheHitRate + ': ' + context.raw + '%'
              : 'Messages: ' + Number(context.raw || 0).toLocaleString()
        } },
      },
      scales: {
        x: { grid: { display: false } },
        yTokens: { position: 'left', beginAtZero: true, ticks: { callback: value => wrFormatTokens(value) } },
        yMessages: { position: 'right', beginAtZero: true, grid: { display: false }, ticks: { color: '#b45309' } },
        yRate: { position: 'right', min: 0, max: 100, grid: { display: false }, ticks: { color: '#15803d', callback: value => value + '%' } },
      },
    },
  });
}

function mrRenderPulse(kpi, series, totalDays, prefix = MR_PREFIX) {
  const labels = dashboardText();
  const el = document.getElementById(prefix + '_pulse');
  if (!el) return;
  const activeDays = (series || []).filter(item => Number(item.tokens || 0) > 0 || Number(item.messages || 0) > 0).length;
  const peak = (series || []).reduce((best, item) => Number(item.tokens || 0) > Number(best.tokens || 0) ? item : best, {});
  const cards = [
    {
      label: labels.monthlyPulseRhythm,
      value: activeDays + '/' + totalDays,
      note: peak && peak.date ? 'Peak ' + (peak.displayDate || peak.date) : labels.noData,
      tone: 'blue',
    },
  ];
  el.innerHTML = cards.map(card => `
    <div class="mr-pulse-card mr-pulse-${card.tone}">
      <span>${escapeHtml(card.label)}</span>
      <b>${escapeHtml(card.value)}</b>
      <small>${escapeHtml(card.note)}</small>
    </div>
  `).join('');
}

function mrRenderModels(models, prefix = MR_PREFIX) {
  const el = document.getElementById(prefix + '_modelChart');
  if (!el) return;
  const top = (models || []).filter(item => item.tokens > 0).slice(0, 10);
  if (!top.length) return;
  const colors = ['#533afd','#8B5CF6','#D97706','#10B981','#F59E0B','#EF4444','#3B82F6','#EC4899'];
  WR_CHARTS[prefix].model = new Chart(el, {
    type: 'bar',
    data: {
      labels: top.map(item => item.name),
      datasets: [{ data: top.map(item => item.tokens), backgroundColor: top.map((_, index) => colors[index % colors.length]), borderRadius: 6, borderSkipped: false }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: context => wrFormatTokens(context.raw) + ' tokens' } } },
      scales: {
        x: { beginAtZero: true, grid: { color: '#f0f4f8' }, ticks: { callback: value => wrFormatTokens(value) } },
        y: { grid: { display: false }, ticks: { font: { weight: '500', size: 11 }, callback: function(value) { const label = this.getLabelForValue(value); return label.length > 24 ? label.slice(0, 22) + '...' : label; } } },
      },
    },
  });
}

function mrUsageEmoji(row) {
  if (row.emoji) return row.emoji;
  const value = String(row.name || '').toLowerCase();
  if (value.includes('codex')) return '🤖';
  if (value.includes('claude')) return '✳️';
  if (value.includes('gemini')) return '✨';
  if (value.includes('hermes')) return '⚕️';
  return '🦞';
}

function mrRenderUsageList(targetId, rows, metric, formatValue) {
  const labels = dashboardText();
  const el = document.getElementById(targetId);
  if (!el) return;
  if (!rows.length) { el.innerHTML = '<div class="wr-empty">' + escapeHtml(labels.noData) + '</div>'; return; }
  const maxValue = Math.max(1, ...rows.map(row => row.stats[metric] || 0));
  el.innerHTML = '<div class="mr-usage-list">' + rows.map(row => {
    const value = row.stats[metric] || 0;
    const width = Math.max(value ? 3 : 0, value / maxValue * 100);
    return `<div><div class="mr-usage-row"><span class="mr-usage-emoji">${mrUsageEmoji(row)}</span><span class="mr-usage-name">${escapeHtml(row.name)}</span><span class="mr-usage-value">${formatValue(value, row.stats)}</span></div><div class="mr-usage-bar"><span style="width:${width}%"></span></div></div>`;
  }).join('') + '</div>';
}

function mrRenderUsageLists(workspaces, agents, totalDays, prefix = MR_PREFIX) {
  const labels = dashboardText();
  const note = document.getElementById(prefix + '_usageNote');
  const rows = Array.isArray(workspaces) && workspaces.length
    ? workspaces.map(item => ({ name: item.name, emoji: item.emoji, stats: item }))
    : Object.entries(agents || {}).map(([name, stats]) => ({ name, stats }));
  if (note) note.textContent = Array.isArray(workspaces) && workspaces.length
    ? labels.workspaceUsageNote
    : labels.agentFallbackUsageNote;
  mrRenderUsageList(prefix + '_usageTokens', rows.slice().sort((a, b) => b.stats.tokens - a.stats.tokens), 'tokens', value => wrFormatTokens(value));
  mrRenderUsageList(prefix + '_usageMessages', rows.slice().sort((a, b) => b.stats.messages - a.stats.messages), 'messages', value => Number(value).toLocaleString());
  mrRenderUsageList(prefix + '_usageDays', rows.slice().sort((a, b) => b.stats.days_active - a.stats.days_active), 'days_active',
    (value, stats) => labels.dayRatio(value, stats.total_days || totalDays));
}

function mrRenderKnowledge(period, prefix = MR_PREFIX, periodLabel = null) {
  const labels = dashboardText();
  periodLabel = periodLabel || labels.monthlyKnowledgePeriod;
  const el = document.getElementById(prefix + '_knowledge');
  if (!el) return;
  const rag = (period && period.rag) || {};
  const memory = (period && period.memory) || {};
  const delta = (item, countUnit) => item.deltaAvailable
    ? `${item.deltaCount >= 0 ? '+' : ''}${Number(item.deltaCount || 0).toLocaleString()} ${countUnit} · ${item.deltaSizeMB >= 0 ? '+' : ''}${item.deltaSizeMB || 0} MB`
    : labels.noComparableSnapshot;
  const ragRange = rag.deltaAvailable ? labels.snapshotDelta(periodLabel, rag.from, rag.to) : labels.periodDelta(periodLabel);
  const memoryRange = memory.deltaAvailable ? labels.snapshotDelta(periodLabel, memory.from, memory.to) : labels.periodDelta(periodLabel);
  el.innerHTML = `
    <div class="mr-knowledge-total"><span>${escapeHtml(labels.currentRagTotal)}</span><b>${Number(rag.currentCount || 0).toLocaleString()} ${escapeHtml(labels.countItems)} · ${rag.currentSizeMB || 0} MB</b></div>
    <div class="mr-knowledge-delta"><span>${escapeHtml(ragRange)}</span><b>${delta(rag, labels.countItems)}</b></div>
    <div class="mr-knowledge-total secondary"><span>${escapeHtml(labels.currentMemoryTotal)}</span><b>${Number(memory.currentCount || 0).toLocaleString()} ${escapeHtml(labels.fileItems)} · ${memory.currentSizeMB || 0} MB</b></div>
    <div class="mr-knowledge-delta"><span>${escapeHtml(memoryRange)}</span><b>${delta(memory, labels.fileItems)}</b></div>`;
}

function mrRenderHeatmap(heatmapData) {
  const dates = (heatmapData && heatmapData.dates) || [];
  const periods = (heatmapData && heatmapData.periods) || [];
  const values = {};
  periods.forEach(period => { values[period.label] = period.values || []; });
  const trend = dates.map((date, index) => ({
    date,
    slots: {
      '上午': (values['上午'] || [])[index] || 0,
      '下午': (values['下午'] || [])[index] || 0,
      '晚上': (values['晚上'] || [])[index] || 0,
      '凌晨': (values['凌晨'] || [])[index] || 0,
    },
  }));
  renderHeatmap(trend, 'mr_work', true);
}

// ── WR Render helpers ──
function wrRenderKPI(p, kpi) {
  const labels = dashboardText();
  const items = [
    { label: 'Total Tokens', value: wrFormatTokens(kpi.totalTokens), sub: (kpi.totalApiCalls||0).toLocaleString() + ' API calls' },
    { label: 'Messages', value: (kpi.totalMessages||0).toLocaleString(), sub: (kpi.totalTokens ? wrFormatTokens(kpi.totalTokens) + ' tokens' : '') },
    { label: labels.activeSessions, value: (kpi.activeSessions||0).toLocaleString(), sub: (kpi.totalSessions ? kpi.activeSessions + '/' + kpi.totalSessions : labels.accumulated) },
    { label: labels.cacheHitRate, value: (kpi.cacheHitRate||0) + '%', color: kpi.cacheHitRate > 50 ? 'var(--success)' : 'var(--ruby)' },
    { label: labels.cronJobsLabel, value: (kpi.cronSuccessRate||0) + '%', color: kpi.cronSuccessRate > 90 ? 'var(--success)' : 'var(--ruby)', sub: labels.successRate },
    { label: 'Agents', value: kpi.agentCount || 0, sub: labels.active },
  ];
  const el = document.getElementById(p + '_kpi');
  if (el) el.innerHTML = items.map(it => `
    <div class="wr-kpi-card">
      <div class="wr-kpi-label">${it.label}</div>
      <div class="wr-kpi-value" ${it.color ? 'style="color:'+it.color+'"' : ''}>${it.value}</div>
      ${it.sub ? '<div class="wr-kpi-sub">'+it.sub+'</div>' : ''}
    </div>`).join('');
}

function wrRenderTokenTrend(p, series) {
  const el = document.getElementById(p + '_tokenChart');
  if (!el || !series || !series.length) return;
  const labels = series.map(s => s.displayDate || s.date);
  WR_CHARTS[p].token = new Chart(el, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Total Tokens',
        data: series.map(s => s.tokens || 0),
        backgroundColor: '#533afd',
        borderRadius: 4,
        borderSkipped: false,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true, ticks: { callback: v => wrFormatTokens(v) } },
      },
    },
  });
}

// Messages-only line chart (Sessions removed - unreliable cumulative data)
function wrRenderSessionMsgTrend(p, series) {
  const el = document.getElementById(p + '_sessionChart');
  if (!el || !series || !series.length) return;
  const labels = series.map(s => s.displayDate || s.date);
  WR_CHARTS[p].session = new Chart(el, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Messages', data: series.map(s => s.tokens > 0 ? (s.messages || 0) : null),
          borderColor: '#7c3aed', backgroundColor: 'rgba(124,58,237,0.08)',
          fill: true, tension: 0.3, pointRadius: 4, pointBackgroundColor: '#7c3aed',
          spanGaps: false,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top', labels: { boxWidth: 12, padding: 12, font: { size: 11 } } } },
      scales: {
        y: { beginAtZero: true, title: { display: true, text: 'Messages', font: { size: 11 } }, grid: { color: 'rgba(0,0,0,0.04)' } },
        x: { grid: { display: false } },
      },
    },
  });
}

// P1-3: Cache hit rate area chart
function wrRenderCacheTrend(p, series) {
  const el = document.getElementById(p + '_cacheChart');
  if (!el || !series || !series.length) return;
  const labels = series.map(s => s.displayDate || s.date);
  const rates = series.map(s => s.cacheHitRate || 0);
  const bgColors = rates.map(r => r > 60 ? 'rgba(21,190,83,0.3)' : r > 30 ? 'rgba(245,158,11,0.3)' : 'rgba(234,34,97,0.3)');
  const borderColors = rates.map(r => r > 60 ? '#15be53' : r > 30 ? '#f59e0b' : '#ea2261');
  WR_CHARTS[p].cache = new Chart(el, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Cache Hit Rate (%)', data: rates.map((r, i) => series[i].tokens > 0 ? r : null),
        backgroundColor: 'rgba(21,190,83,0.1)', borderColor: '#15be53',
        fill: true, tension: 0.3, pointRadius: 5, pointBackgroundColor: borderColors,
        spanGaps: false,
        segment: {
          borderColor: ctx => {
            const v = ctx.p1.parsed.y;
            return v > 60 ? '#15be53' : v > 30 ? '#f59e0b' : '#ea2261';
          },
          backgroundColor: ctx => {
            const v = ctx.p1.parsed.y;
            return v > 60 ? 'rgba(21,190,83,0.15)' : v > 30 ? 'rgba(245,158,11,0.15)' : 'rgba(234,34,97,0.15)';
          },
        },
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, max: 100, ticks: { callback: v => v + '%' } },
        x: { grid: { display: false } },
      },
    },
  });
}

// P1-4: Agent activity days
function wrRenderAgentDays(p, agents, totalDays) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_agentDays');
  if (!el || !agents) return;
  const names = Object.keys(agents).sort((a, b) => agents[b].days_active - agents[a].days_active);
  if (!names.length) { el.innerHTML = '<div class="wr-empty">' + escapeHtml(labels.noData) + '</div>'; return; }
  el.innerHTML = '<div style="display:flex;flex-direction:column;gap:10px">' + names.map(n => {
    const days = agents[n].days_active || 0;
    const total = agents[n].total_days || totalDays || 7;
    const rate = agents[n].active_rate || 0;
    const pct = Math.round(days / total * 100);
    const color = pct >= 100 ? 'var(--success)' : pct >= 50 ? '#f59e0b' : 'var(--slate)';
    return `<div style="display:flex;align-items:center;gap:12px">
      <span style="min-width:100px;font-size:13px;font-weight:500;color:var(--navy)">${n}</span>
      <div class="wr-progress-bar" style="flex:1;height:10px">
        <div class="wr-progress-fill" style="width:${pct}%;background:${color}"></div>
      </div>
      <span style="min-width:90px;text-align:right;font-size:12px;color:var(--slate)">${escapeHtml(labels.dayRatio(days, total))} (${rate}%)</span>
    </div>`;
  }).join('') + '</div>';
}

function wrRenderModelUsage(p, models) {
  const el = document.getElementById(p + '_modelChart');
  if (!el || !models || !models.length) return;
  const top = models.slice(0, 8);
  WR_CHARTS[p].model = new Chart(el, {
    type: 'bar',
    data: {
      labels: top.map(m => m.model),
      datasets: [{ label: 'Tokens', data: top.map(m => m.tokens), backgroundColor: '#533afd88', borderRadius: 4 }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { beginAtZero: true, ticks: { callback: v => wrFormatTokens(v) } }, y: { grid: { display: false } } },
    },
  });
}

function wrRenderAgentActivity(p, agents) {
  const el = document.getElementById(p + '_agentChart');
  if (!el || !agents) return;
  const names = Object.keys(agents).sort((a, b) => agents[b].messages - agents[a].messages);
  if (!names.length) return;
  WR_CHARTS[p].agent = new Chart(el, {
    type: 'doughnut',
    data: {
      labels: names,
      datasets: [{ data: names.map(n => agents[n].messages), backgroundColor: names.map(wrAgentColor), borderWidth: 0 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'right', labels: { boxWidth: 12, padding: 10, font: { size: 11 } } } },
    },
  });
}

function wrRenderTaskStats(p, ts) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_task');
  if (!el || !ts) return;
  const rate = ts.completionRate || 0;
  const color = rate > 70 ? 'var(--success)' : rate > 40 ? '#f59e0b' : 'var(--ruby)';
  el.innerHTML = `
    <div class="wr-stats-row" style="margin-bottom:12px">
      <div class="wr-stat-item"><div class="wr-stat-label">${escapeHtml(labels.completed)}</div><div class="wr-stat-value">${ts.completed||0}</div></div>
      <div class="wr-stat-item"><div class="wr-stat-label">${escapeHtml(labels.inProgress)}</div><div class="wr-stat-value">${ts.inProgress||0}</div></div>
    </div>
    <div class="wr-progress-bar"><div class="wr-progress-fill" style="width:${rate}%;background:${color}"></div></div>
    <div style="font-size:12px;color:var(--slate);margin-top:6px">${escapeHtml(labels.completionRate)} ${rate}%</div>`;
}

function wrRenderWorkloadComparison(p, comparison) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_workload');
  if (!el) return;
  const signed = (value, formatter) => {
    const n = Number(value || 0);
    return (n >= 0 ? '+' : '-') + formatter(Math.abs(n));
  };
  const metrics = [
    { key: 'totalTokens', label: labels.totalTokenMetric, format: value => wrFormatTokens(value || 0) },
    { key: 'totalMessages', label: labels.totalMessageMetric, format: value => Number(value || 0).toLocaleString() },
    { key: 'cacheHitRate', label: labels.cacheRateMetric, format: value => Number(value || 0).toFixed(1) + '%' },
  ];
  const rowHtml = metrics.map(metric => {
    const item = (comparison && comparison[metric.key]) || {};
    const delta = Number(item.delta || 0);
    const hasDelta = !!item.deltaAvailable;
    const pct = item.percentDelta === null || item.percentDelta === undefined ? '' : ` (${item.percentDelta >= 0 ? '+' : ''}${item.percentDelta}%)`;
    const deltaText = hasDelta
      ? `${signed(delta, metric.format)}${pct}`
      : labels.noComparablePeriod;
    const color = !hasDelta ? 'var(--slate)' : delta >= 0 ? 'var(--success)' : 'var(--ruby)';
    return `
      <div class="wr-workload-row">
        <span>${escapeHtml(metric.label)}</span>
        <b>${escapeHtml(metric.format(item.current || 0))}</b>
        <small style="color:${color}">${escapeHtml(deltaText)}</small>
      </div>`;
  }).join('');
  el.innerHTML = '<div class="wr-workload-list">' + rowHtml + '</div><div class="wr-workload-note">' + escapeHtml(labels.comparedWithPrevious) + '</div>';
}

function wrRenderCronStats(p, cs) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_cron');
  if (!el || !cs) return;
  el.innerHTML = `
    <div class="wr-stats-row" style="margin-bottom:12px">
      <div class="wr-stat-item"><div class="wr-stat-label">${escapeHtml(labels.successLabel)}</div><div class="wr-stat-value" style="color:var(--success)">${cs.success||0}</div></div>
      <div class="wr-stat-item"><div class="wr-stat-label">${escapeHtml(labels.failedLabel)}</div><div class="wr-stat-value" style="color:var(--ruby)">${cs.failed||0}</div></div>
    </div>
    <div class="wr-progress-bar"><div class="wr-progress-fill" style="width:${cs.rate||0}%;background:${cs.rate>90?'var(--success)':'var(--ruby)'}"></div></div>
    <div style="font-size:12px;color:var(--slate);margin-top:6px">${escapeHtml(labels.successRate)} ${cs.rate||0}%</div>`;
}

function wrRenderKnowledge(p, rag, mem) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_knowledge');
  if (!el) return;
  el.innerHTML = `
    <div class="wr-stats-row" style="margin-bottom:8px">
      <div class="wr-stat-item"><div class="wr-stat-label">${escapeHtml(labels.novaRagEntries)}</div><div class="wr-stat-value">${rag?.entries||0}</div></div>
      <div class="wr-stat-item"><div class="wr-stat-label">${escapeHtml(labels.novaRagSize)}</div><div class="wr-stat-value">${rag?.sizeMB||0} MB</div></div>
    </div>
    <div class="wr-stats-row">
      <div class="wr-stat-item"><div class="wr-stat-label">${escapeHtml(labels.memoryFiles)}</div><div class="wr-stat-value">${mem?.sessionFiles||0}</div></div>
      <div class="wr-stat-item"><div class="wr-stat-label">${escapeHtml(labels.memorySize)}</div><div class="wr-stat-value">${mem?.totalSizeMB||0} MB</div></div>
    </div>`;
}

function wrRenderTopics(p, topics) {
  const el = document.getElementById(p + '_topics');
  const labels = dashboardText();
  if (!el || !topics || !topics.length) { if (el) el.innerHTML = '<div class="wr-empty">' + escapeHtml(labels.noTopics) + '</div>'; return; }
  const maxCount = Math.max(1, ...topics.map(t => Number(t.count || 0)));
  el.innerHTML = '<div class="wr-topic-list">' + topics.map((t, index) => {
    const topic = escapeHtml(t.topic || t.title || '');
    const count = Number(t.count || 0);
    const reason = escapeHtml(t.reason || '');
    const countHtml = count > 0 ? `<span class="count">${escapeHtml(labels.strength)} ${count}</span>` : '<span class="count">' + escapeHtml(labels.mentioned) + '</span>';
    const width = Math.max(count ? 12 : 5, count / maxCount * 100);
    return topic ? `<div class="wr-topic-tag">
      <span class="wr-topic-rank">${index + 1}</span>
      <span class="wr-topic-main"><span class="wr-topic-name">${topic}</span>${reason ? `<span class="wr-topic-reason">${reason}</span>` : ''}</span>
      ${countHtml}
      <span class="wr-topic-meter"><span style="width:${width}%"></span></span>
    </div>` : '';
  }).join('') + '</div>';
}

function wrRenderInlineMarkdown(text) {
  return escapeHtml(text || '').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function wrPeriodSummaryMarkdownHtml(markdown) {
  const lines = String(markdown || '').split(/\r?\n/);
  const html = [];
  let listOpen = false;
  let orderedListOpen = false;
  let sectionOpen = false;
  const closeList = () => {
    if (listOpen) {
      html.push('</ul>');
      listOpen = false;
    }
    if (orderedListOpen) {
      html.push('</ol>');
      orderedListOpen = false;
    }
  };
  const closeSection = () => {
    closeList();
    if (sectionOpen) {
      html.push('</section>');
      sectionOpen = false;
    }
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      closeList();
      continue;
    }
    if (line.startsWith('# ') && !sectionOpen && html.length === 0) {
      html.push('<div class="wr-narrative-md-title">' + wrRenderInlineMarkdown(line.slice(2)) + '</div>');
      continue;
    }
    if (line.startsWith('## ')) {
      closeSection();
      html.push('<section class="wr-narrative-md-section"><h3>' + wrRenderInlineMarkdown(line.slice(3)) + '</h3>');
      sectionOpen = true;
      continue;
    }
    if (line.startsWith('### ')) {
      closeList();
      html.push('<h4>' + wrRenderInlineMarkdown(line.slice(4)) + '</h4>');
      continue;
    }
    if (line.startsWith('- ')) {
      if (orderedListOpen) {
        html.push('</ol>');
        orderedListOpen = false;
      }
      if (!listOpen) {
        html.push('<ul>');
        listOpen = true;
      }
      html.push('<li>' + wrRenderInlineMarkdown(line.slice(2)) + '</li>');
      continue;
    }
    const ordered = line.match(/^(\d+)\.\s+(.+)$/);
    if (ordered) {
      if (listOpen) {
        html.push('</ul>');
        listOpen = false;
      }
      if (!orderedListOpen) {
        html.push('<ol>');
        orderedListOpen = true;
      }
      html.push('<li>' + wrRenderInlineMarkdown(ordered[2]) + '</li>');
      continue;
    }
    if (line.startsWith('> ')) {
      closeList();
      html.push('<blockquote>' + wrRenderInlineMarkdown(line.slice(2)) + '</blockquote>');
      continue;
    }
    closeList();
    html.push('<p>' + wrRenderInlineMarkdown(line) + '</p>');
  }
  closeSection();
  return html.join('');
}

function wrPeriodSummaryMetaHtml(freshness) {
  if (!freshness) return '';
  const labels = dashboardText();
  const source = freshness.source || 'unknown';
  const status = freshness.status || '';
  const generated = freshness.generatedAt ? String(freshness.generatedAt).replace('T', ' ').slice(0, 19) : '';
  const bits = [
    source ? labels.source + ' ' + source : '',
    status ? labels.status + ' ' + status : '',
    generated ? labels.generated + ' ' + generated : ''
  ].filter(Boolean);
  return bits.length ? '<div class="wr-narrative-meta">' + bits.map(escapeHtml).join(' · ') + '</div>' : '';
}

function wrSplitPeriodSummaryMarkdown(markdown) {
  const overviewTitles = ['本周期总览', 'Period Overview', '本周总结', '本月总结', 'Weekly Summary', 'Monthly Summary'];
  const careTitles = ['关怀与鼓励', 'Care and Encouragement'];
  const quoteLines = [];
  const cleaned = String(markdown || '').split(/\r?\n/).filter(line => {
    if (line.trim().startsWith('> ')) {
      quoteLines.push(line.trim().slice(2).trim());
      return false;
    }
    return true;
  }).join('\n');
  const sections = [];
  let current = { title: '', lines: [] };
  const push = () => {
    const body = current.lines.join('\n').trim();
    if (current.title || body) sections.push({ title: current.title, body });
  };
  for (const raw of cleaned.split(/\r?\n/)) {
    const h2 = raw.match(/^##\s+(.+?)\s*$/);
    if (h2) {
      push();
      current = { title: h2[1].trim(), lines: [] };
      continue;
    }
    current.lines.push(raw);
  }
  push();
  return {
    quote: quoteLines.join(' '),
    overview: sections.find(s => overviewTitles.includes(s.title)),
    care: sections.find(s => careTitles.includes(s.title)),
    details: sections.filter(s => s.title && !overviewTitles.includes(s.title) && !careTitles.includes(s.title)),
  };
}

function wrCleanSummaryQuoteLine(line) {
  return String(line || '')
    .trim()
    .replace(/^[-*]\s+/, '')
    .replace(/^\d+[.)、]\s+/, '')
    .replace(/\*\*/g, '')
    .trim();
}

function wrExtractSummaryQuote(split) {
  if (split && split.quote) return split.quote;
  const careBody = split && split.care ? split.care.body : '';
  const lines = String(careBody || '').split(/\r?\n/).map(wrCleanSummaryQuoteLine).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i];
    const quoted = line.match(/[“"]([^”"]{6,120})[”"]/);
    if (quoted) return quoted[1].trim();
  }
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].replace(/^一些名言[:：]\s*/, '').trim();
    if (line.length >= 8 && line.length <= 140) return line;
  }
  return '';
}

function wrSummaryFocusHtml(part, quote) {
  const quoteText = wrCleanSummaryQuoteLine(quote || '');
  const paragraphs = [];
  for (const raw of String(part && part.body ? part.body : '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('### ')) continue;
    const cleaned = wrCleanSummaryQuoteLine(line);
    if (!cleaned) continue;
    if (quoteText && (cleaned === quoteText || cleaned.includes(quoteText))) continue;
    if (/^一些名言[:：]/.test(cleaned)) continue;
    paragraphs.push('<p>' + wrRenderInlineMarkdown(cleaned) + '</p>');
  }
  return paragraphs.join('');
}

function wrSummaryDetailsTitle(p) {
  const lang = dashboardLanguageProfile();
  if (p === MR_PREFIX && MR_CURRENT_MONTH) {
    const parts = MR_CURRENT_MONTH.split('-');
    const year = parts[0] || '';
    const month = parts[1] || '';
    return lang === 'en'
      ? `${year}-${month} Monthly Summary`
      : `${year}年${month}月度总结`;
  }
  const match = String(p || '').match(/^wr_(\d{4})_W(\d{1,2})$/);
  if (match) {
    const reportId = match[1] + '-W' + match[2].padStart(2, '0');
    const start = wrMondayOfWeek(reportId);
    const month = start ? start.slice(5, 7) : '';
    return lang === 'en'
      ? `${month} W${match[2].padStart(2, '0')} Weekly Summary`
      : `${month}月W${match[2].padStart(2, '0')}周度总结`;
  }
  return lang === 'en' ? 'Summary Details' : '总结详情';
}

function wrRenderPeriodSummary(p, summary, freshness) {
  const el = document.getElementById(p + '_periodSummary');
  const quoteEl = document.getElementById(p + '_summaryQuote');
  const detailsEl = document.getElementById(p + '_summaryDetails');
  const detailsSection = document.getElementById(p + '_summaryDetailsSection');
  if (quoteEl) quoteEl.style.display = 'none';
  if (detailsSection) detailsSection.style.display = 'none';
  if (detailsEl) detailsEl.innerHTML = '';
  if (!el) return;
  if (!summary) {
    const labels = dashboardText();
    const suffix = freshness && freshness.refreshRequired ? labels.clickGenerateSummary : labels.sentenceEnd;
    el.innerHTML = '<div class="wr-narrative-placeholder">' + escapeHtml(labels.noSummaryPrefix + suffix) + '</div>';
    return;
  }
  if (summary.markdown) {
    const labels = dashboardText();
    if (!quoteEl && !detailsEl && !detailsSection) {
      el.innerHTML = wrPeriodSummaryMetaHtml(freshness) + (wrPeriodSummaryMarkdownHtml(summary.markdown) || '<div class="wr-narrative-placeholder">' + escapeHtml(labels.emptySummary) + '</div>');
      return;
    }
    const split = wrSplitPeriodSummaryMarkdown(summary.markdown);
    const quote = wrExtractSummaryQuote(split);
    if (quoteEl && quote) {
      quoteEl.innerHTML = '<span>“</span><em>' + wrRenderInlineMarkdown(quote) + '</em><span>”</span>';
      quoteEl.style.display = '';
    }
    const summaryParts = [split.overview, split.care]
      .filter(part => part && part.body)
      .map(part => wrSummaryFocusHtml(part, quote))
      .filter(Boolean)
      .map(html => '<div class="wr-summary-focus-block">' + html + '</div>')
      .join('');
    el.innerHTML = wrPeriodSummaryMetaHtml(freshness) + (summaryParts || '<div class="wr-narrative-placeholder">' + escapeHtml(labels.emptySummary) + '</div>');
    if (detailsEl && detailsSection && split.details.length) {
      detailsEl.innerHTML = '<div class="wr-summary-details-title">' + escapeHtml(wrSummaryDetailsTitle(p)) + '</div>' + split.details
        .map((part, index) => '<div class="wr-summary-detail-block"><div class="wr-summary-detail-index">' + String(index + 1).padStart(2, '0') + '</div><div class="wr-summary-detail-body">' + wrPeriodSummaryMarkdownHtml('## ' + part.title + '\n\n' + part.body) + '</div></div>')
        .join('');
      detailsSection.style.display = '';
    }
    return;
  }
  const lead = escapeHtml(summary.lead || '');
  const highlights = (summary.highlights || []).map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  const lessons = (summary.lessons || []).map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  const labels = dashboardText();
  el.innerHTML = [
    wrPeriodSummaryMetaHtml(freshness),
    lead ? '<div class="wr-narrative-lead">' + lead + '</div>' : '',
    highlights ? '<div class="wr-narrative-group"><div class="wr-narrative-group-title">' + escapeHtml(labels.highlights) + '</div><ul>' + highlights + '</ul></div>' : '',
    lessons ? '<div class="wr-narrative-group"><div class="wr-narrative-group-title">' + escapeHtml(labels.retrospective) + '</div><ul>' + lessons + '</ul></div>' : ''
  ].join('') || '<div class="wr-narrative-placeholder">' + escapeHtml(labels.emptySummary) + '</div>';
}

function wrRenderSummaryTopics(p, items) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_summary');
  if (!el || !items || !items.length) { if (el) el.innerHTML = '<div class="wr-empty">' + escapeHtml(labels.noData) + '</div>'; return; }
  const stripBold = (s) => (s || '').replace(/\*\*/g, '').trim();
  const cleanItems = items.map(it => ({
    ...it,
    title: stripBold(it.title || it.topic),
    items: (it.items || []).map(stripBold),
  }));
  const dateSet = new Set(cleanItems.map(it => it.date || 'unknown'));
  const dateList = Array.from(dateSet).sort().reverse();
  const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const fmtDate = (d) => {
    if (d === 'unknown') return labels.unknown;
    const dt = new Date(d + 'T00:00:00');
    return (dt.getMonth()+1) + '/' + dt.getDate() + ' ' + (dayNames[dt.getDay()] || '');
  };
  const renderTopic = (it) => {
    const title = escapeHtml(it.title || '');
    const children = (it.items || []).map(escapeHtml);
    const preview = children[0] || '';
    return '<div class="wr-summary-topic-item"><div class="wr-summary-topic-title" onclick="this.parentElement.classList.toggle(\'open\')"><span class="wr-summary-topic-heading">' + title + (preview ? '<small class="wr-summary-topic-preview">' + preview + '</small>' : '') + '</span><small class="wr-summary-topic-count">' + children.length + ' ' + escapeHtml(labels.itemUnit) + '</small></div>' +
      (children.length ? '<ul class="wr-summary-topic-items">' + children.map(i => '<li>' + i + '</li>').join('') + '</ul>' : '') +
    '</div>';
  };
  window['__wrSummaryItems_' + p] = cleanItems;
  window['__wrSummaryDates_' + p] = dateList;
  const filterId = p + '_summaryFilter';
  let filterHtml = '<div class="wr-summary-filter" id="' + filterId + '">' +
    '<button class="wr-summary-filter-pill active" data-date="all" onclick="wrFilterSummaryByDate(\'' + p + '\',\'all\')">' + escapeHtml(labels.all) + '</button>' +
    dateList.map(d => '<button class="wr-summary-filter-pill" data-date="' + d + '" onclick="wrFilterSummaryByDate(\'' + p + '\',\'' + d + '\')">' + fmtDate(d) + '</button>').join('') + '</div>';
  const contentId = p + '_summaryContent';
  const renderItems = (filterDate) => {
    const filtered = filterDate === 'all' ? cleanItems : cleanItems.filter(it => it.date === filterDate);
    if (!filtered.length) return '<div class="wr-empty">' + escapeHtml(labels.noMatches) + '</div>';
    const byDate = {};
    for (const it of filtered) {
      const dd = it.date || 'unknown';
      if (!byDate[dd]) byDate[dd] = [];
      byDate[dd].push(it);
    }
    return Object.entries(byDate).map(([date, list]) =>
      '<div class="wr-summary-date-group"><div class="wr-summary-date-label">' + escapeHtml(date) + '</div>' +
      list.map(renderTopic).join('') + '</div>'
    ).join('');
  };
  el.innerHTML = filterHtml + '<div id="' + contentId + '">' + renderItems('all') + '</div>';
}

function wrFilterSummaryByDate(p, date) {
  const labels = dashboardText();
  const items = window['__wrSummaryItems_' + p] || [];
  const dateList = window['__wrSummaryDates_' + p] || [];
  const filterEl = document.getElementById(p + '_summaryFilter');
  if (filterEl) {
    filterEl.querySelectorAll('.wr-summary-filter-pill').forEach(s => {
      s.classList.toggle('active', s.dataset.date === date);
    });
  }
  const contentEl = document.getElementById(p + '_summaryContent');
  if (!contentEl) return;
  const filtered = date === 'all' ? items : items.filter(it => it.date === date);
  if (!filtered.length) { contentEl.innerHTML = '<div class="wr-empty">' + escapeHtml(labels.noMatches) + '</div>'; return; }
  const byDate = {};
  for (const it of filtered) {
    const dd = it.date || 'unknown';
    if (!byDate[dd]) byDate[dd] = [];
    byDate[dd].push(it);
  }
  contentEl.innerHTML = Object.entries(byDate).map(([d, list]) =>
    '<div class="wr-summary-date-group"><div class="wr-summary-date-label">' + escapeHtml(d) + '</div>' +
    list.map(it => {
      const title = escapeHtml(it.title || '');
      const children = (it.items || []).map(escapeHtml);
      const preview = children[0] || '';
      return '<div class="wr-summary-topic-item"><div class="wr-summary-topic-title" onclick="this.parentElement.classList.toggle(\'open\')"><span class="wr-summary-topic-heading">' + title + (preview ? '<small class="wr-summary-topic-preview">' + preview + '</small>' : '') + '</span><small class="wr-summary-topic-count">' + children.length + ' ' + escapeHtml(labels.itemUnit) + '</small></div>' +
        (children.length ? '<ul class="wr-summary-topic-items">' + children.map(i => '<li>' + i + '</li>').join('') + '</ul>' : '') +
      '</div>';
    }).join('') + '</div>'
  ).join('');
}

function wrRenderAgentWork(p, workList, heatmapData) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_work');
  if (!el) return;
  const dates = (heatmapData && heatmapData.dates) || [];
  const periods = (heatmapData && heatmapData.periods) || [];
  if (!dates.length || !periods.length) { el.innerHTML = '<div class="wr-empty">' + escapeHtml(labels.noData) + '</div>'; return; }

  const dayNames = labels.dayNames || ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const fmtTok = (v) => { v = Number(v)||0; return v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? (v/1e3).toFixed(0)+'K' : v > 0 ? String(v) : ''; };
  // Find max across all cells
  let maxVal = 1;
  for (const pd of periods) { for (const v of (pd.values||[])) { if (v > maxVal) maxVal = v; } }
  const getColor = (v) => {
    const r = v / maxVal;
    if (r === 0) return 'rgba(83,58,253,0.03)';
    if (r < 0.1) return 'rgba(83,58,253,0.08)';
    if (r < 0.25) return 'rgba(83,58,253,0.18)';
    if (r < 0.5) return 'rgba(83,58,253,0.35)';
    if (r < 0.75) return 'rgba(83,58,253,0.55)';
    return 'rgba(83,58,253,0.78)';
  };
  const getTextColor = (v) => (v / maxVal > 0.35) ? 'white' : 'var(--navy)';

  // Column headers: dates
  const numDays = dates.length;
  let colHeaders = '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">' +
    '<div style="width:52px;flex-shrink:0"></div>' +
    '<div style="display:flex;gap:4px;flex:1">' +
    dates.map(d => {
      const dt = new Date(d + 'T00:00:00');
      const day = dt.getDate();
      const label = day + ' ' + (dayNames[dt.getDay()] || '');
      return '<div style="flex:1;text-align:center;font-size:11px;font-weight:600;color:var(--slate);padding-bottom:4px">' + label + '</div>';
    }).join('') + '</div></div>';

  // Row labels with range info
  const rowMeta = [
    {label:(labels.timeSlotLabels || [])[0] || 'Morning', range:'04:00-12:00'},
    {label:(labels.timeSlotLabels || [])[1] || 'Afternoon', range:'12:00-18:00'},
    {label:(labels.timeSlotLabels || [])[2] || 'Evening', range:'18:00-00:00'},
    {label:(labels.timeSlotLabels || [])[3] || 'Late night', range:'00:00-04:00'},
  ];

  let rows = '';
  for (let ri = 0; ri < periods.length; ri++) {
    const pd = periods[ri];
    const meta = rowMeta[ri] || {};
    let cells = '';
    for (let ci = 0; ci < numDays; ci++) {
      const v = (pd.values && pd.values[ci]) || 0;
      const display = fmtTok(v) || '–';
      cells += '<div style="flex:1;padding:10px 2px;text-align:center;background:' + getColor(v) + ';color:' + getTextColor(v) + ';font-size:11px;font-weight:500;transition:transform 0.1s" title="' + dates[ci] + ' ' + meta.label + ': ' + (v||0).toLocaleString() + ' tokens" onmouseenter="this.style.transform=\'scale(1.06)\'" onmouseleave="this.style.transform=\'scale(1)\'">' + display + '</div>';
    }
    rows += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">' +
      '<div style="width:52px;flex-shrink:0;text-align:right;font-size:11px;font-weight:600;color:var(--navy)">' + meta.label + '</div>' +
      '<div style="display:flex;gap:4px;flex:1">' + cells + '</div></div>';
  }

  // Legend
  let legend = '<div style="display:flex;align-items:center;gap:10px;margin-top:8px;font-size:10px;color:var(--slate)">' +
    '<span>' + escapeHtml(labels.lowActivity) + '</span>' +
    '<div style="display:flex;gap:2px">' + [0.03,0.08,0.18,0.35,0.55,0.78].map(a => '<div style="width:18px;height:10px;background:rgba(83,58,253,'+a+')"></div>').join('') + '</div>' +
    '<span>' + escapeHtml(labels.highActivity) + '</span></div>';

  el.innerHTML = colHeaders + rows + legend;
}

function wrRenderLessons(p, lessons) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_lessons');
  const cnt = document.getElementById(p + '_lessonCount');
  const filterEl = document.getElementById(p + '_lessonFilter');
  if (!el) return;
  if (!lessons || !lessons.length) { el.innerHTML = '<div class="wr-empty">' + escapeHtml(labels.noLessons) + '</div>'; if (filterEl) filterEl.innerHTML = ''; return; }
  if (cnt) cnt.textContent = '(' + lessons.length + ')';

  // Build filter pills
  const agentNames = [...new Set(lessons.map(l => l.agent || '?'))];
  if (filterEl) {
    filterEl.innerHTML = `<button class="wr-filter-pill active" data-agent="" onclick="wrFilterLessons('${p}','')">${escapeHtml(labels.all)}</button>` +
      agentNames.map(n => `<button class="wr-filter-pill" data-agent="${n}" onclick="wrFilterLessons('${p}','${n}')" style="--pill-color:${wrAgentColor(n)}">${n}</button>`).join('');
  }

  // Store lessons data for filtering
  if (!window._wrLessons) window._wrLessons = {};
  window._wrLessons[p] = lessons;
  wrRenderLessonsList(p, lessons);
}

function wrFilterLessons(p, agent) {
  // Update pill styles
  const filterEl = document.getElementById(p + '_lessonFilter');
  if (filterEl) {
    filterEl.querySelectorAll('.wr-filter-pill').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.agent === agent);
    });
  }
  const all = (window._wrLessons && window._wrLessons[p]) || [];
  const filtered = agent ? all.filter(l => (l.agent || '?') === agent) : all;
  wrRenderLessonsList(p, filtered);
}

function wrRenderLessonsList(p, lessons) {
  const labels = dashboardText();
  const el = document.getElementById(p + '_lessons');
  if (!el) return;
  if (!lessons || !lessons.length) { el.innerHTML = '<div class="wr-empty">' + escapeHtml(labels.noMatchingLessons) + '</div>'; return; }
  el.innerHTML = lessons.map((l, i) => `
    <div class="wr-lesson-card" onclick="this.classList.toggle('open')">
      <div class="wr-lesson-header">
        <span class="wr-lesson-agent" style="background:${wrAgentColor(l.agent || 'unknown')}">${l.agent || '?'}</span>
        <span class="wr-lesson-problem">${l.problem || l.title || ''}</span>
        <span class="wr-lesson-meta">${escapeHtml(l.suggestion ? labels.suggestionLabel : labels.recordLabel)}</span>
        <span class="wr-lesson-expand">▶</span>
        <span class="wr-lesson-date">${l.date || ''}</span>
      </div>
      <div class="wr-lesson-body">
        ${l.context ? '<div>' + l.context + '</div>' : ''}
        ${l.suggestion ? '<div class="wr-lesson-suggestion">' + l.suggestion + '</div>' : ''}
      </div>
    </div>`).join('');
}

// ─── 日记导航动态加载 ────────────────────────────────
async function loadDiaryNav() {
  await ensureDashboardLanguageProfile();
  const labels = dashboardText();
  try {
    const res = await fetch('/api/diary-list');
    if (!res.ok) throw new Error('API ' + res.status);

    const diaries = await res.json();

    // ── 1. 按月分组 → 月内按周分组 ────────────────
    const monthMap = {};  // "2026-04" -> {year, month, weeks:{}, dates:[]}
    for (const d of diaries) {
      const dt = new Date(d.fullDate.replace(/-/g, '/'));
      const year = dt.getFullYear();
      const month = dt.getMonth(); // 0-based
      const mk = `${year}-${String(month + 1).padStart(2,'0')}`;
      if (!monthMap[mk]) monthMap[mk] = {year, month, weeks: {}, dates: []};
      monthMap[mk].dates.push(d);

      // ISO week key
      const week = getISOWeek(dt);
      const wk = `${year}-W${String(week).padStart(2,'0')}`;
      if (!monthMap[mk].weeks[wk]) monthMap[mk].weeks[wk] = [];
      monthMap[mk].weeks[wk].push(d);
    }

    // 生成月份导航 HTML
    const monthNav = document.getElementById('month-nav');
    if (monthNav) {
      const sortedMonths = Object.keys(monthMap).sort().reverse();
      monthNav.innerHTML = sortedMonths.map(mk => {
        const m = monthMap[mk];
        const isCurrentMonth = (mk === new Date().getFullYear() + '-' + String(new Date().getMonth() + 1).padStart(2,'0'));
        const monthLabel = labels.monthLabel(m.year, m.month + 1);
        const monthStart = m.year + '-' + String(m.month + 1).padStart(2,'0') + '-01';
        const daysInMonth = new Date(m.year, m.month + 1, 0).getDate();

        // Sort weeks within month, reverse (newest first)
        const sortedWeeks = Object.keys(m.weeks).sort().reverse();
        const weeksHtml = sortedWeeks.map(wk => {
          const dates = m.weeks[wk].sort((a,b) => b.fullDate.localeCompare(a.fullDate));
          const dateRange = dates.length >= 2
            ? `${dates[dates.length-1].displayDate}～${dates[0].displayDate}`
            : dates[0].displayDate;
          return `<div class="nav-section" style="padding-left:0">
            <div class="nav-section-title nav-section-title-nested" onclick="toggleSection(this)">
              📂 ${wrDisplayWeekId(wk)}（${dateRange}）
              <span class="arrow">›</span>
            </div>
            <div class="nav-items">
              <div class="nav-item" onclick="loadReport('${wk}')">
                <span class="nav-item-dot"></span>
                ${escapeHtml(labels.diaryNavWeeklyOverview)}
              </div>
              ${dates.map(d => `
              <div class="nav-item" onclick="showDiaryByDate('${d.fullDate}', this)">
                <span class="nav-item-dot"></span>
                ${escapeHtml(d.displayDate)} ${escapeHtml(labels.diaryNavDiary)}
              </div>`).join('')}
            </div>
          </div>`;
        }).join('');

        return `<div class="nav-section ${isCurrentMonth ? 'open' : ''}">
          <div class="nav-section-title ${isCurrentMonth ? 'open' : ''}" onclick="toggleSection(this)">
            📅 ${monthLabel}
            <span class="arrow">›</span>
          </div>
          <div class="nav-items ${isCurrentMonth ? 'open' : ''}">
            <div class="nav-item" onclick="loadMonthlyReportById('${mk}')">
              <span class="nav-item-dot"></span>
              📋 ${escapeHtml(labels.diaryNavMonthlyOverview)}
            </div>
            ${weeksHtml}
          </div>
        </div>`;
      }).join('');
    }

    // ── 2. 预建日记页面容器（避免 showPage 找不到 div）──
    const pages = document.getElementById('diary-pages');
    if (pages) {
      for (const d of diaries) {
        const pageId = `page-day-${d.date}`;
        if (!document.getElementById(pageId)) {
          const div = document.createElement('div');
          div.id = pageId;
          div.className = 'page';
          div.innerHTML = `<div class="page-header"><div class="page-title">${escapeHtml(d.displayDate)} ${escapeHtml(labels.diaryNavDiary)}</div><div class="page-subtitle">${escapeHtml(d.fullDate)} · ${escapeHtml(d.dayOfWeek)}</div><div id="diary-weather-${d.date}" class="page-subtitle" style="margin-top:4px;font-size:13px"></div></div><div id="diary-content-${d.date}" class="page-body diary-page-body">${escapeHtml(labels.diaryNavLoading)}</div>`;
          pages.appendChild(div);
        }
      }
    }
  } catch (e) {
    console.error('loadDiaryNav error:', e);
  }
}

// ISO 周号
function getISOWeek(date) {
  const d = new Date(date.getTime());
  d.setHours(0,0,0,0);
  d.setDate(d.getDate() + 3 - (d.getDay() + 6) % 7);
  const week1 = new Date(d.getFullYear(), 0, 4);
  return 1 + Math.round(((d.getTime() - week1.getTime()) / 86400000 - 3 + (week1.getDay() + 6) % 7) / 7);
}

// ─── 日记内容动态渲染 ─────────────────────────────────
// 点击侧边栏日记 → 切换页面并加载内容
async function showDiaryByDate(fullDate, navEl) {
  const labels = dashboardText();
  const date = fullDate.replace('2026-', '').replace('-', '');
  const pageId = `page-day-${date}`;

  // 切换页面
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.nav-item-dot').forEach(d => d.classList.remove('active'));

  const page = document.getElementById(pageId);
  if (page) page.classList.add('active');

  // 高亮侧边栏
  if (navEl) {
    navEl.classList.add('active');
    const dot = navEl.querySelector('.nav-item-dot');
    if (dot) dot.classList.add('active');
  }

  location.hash = pageId;

  // 加载内容
  const contentEl = document.getElementById('diary-content-' + date);
  if (!contentEl) return;
  contentEl.innerHTML = '<div style="padding:20px;color:var(--gray)">' + escapeHtml(labels.loadingDiary) + '</div>';

  try {
    const res = await fetch(`/api/diary/${fullDate}`);
    if (!res.ok) { contentEl.innerHTML = labels.updateFailed; return; }
    const d = await res.json();
    if (diaryNeedsRefresh(d.dataFreshness)) {
      renderDiaryRefreshNotice(contentEl, fullDate, date, navEl);
      return;
    }
    renderDiaryContent(date, d);
  } catch (e) {
    contentEl.innerHTML = '<div style="padding:20px;color:var(--error)">' + escapeHtml(labels.updateFailed + ': ' + e.message) + '</div>';
  }
}

function diaryNeedsRefresh(freshness) {
  return !!(freshness && freshness.diaryPage && freshness.diaryPage.source === 'snapshot-missing');
}

function renderDiaryRefreshNotice(contentEl, fullDate, shortDate, navEl) {
  const labels = dashboardText();
  const buttonId = 'diary_' + shortDate + '_rebuildSnapshot';
  contentEl.innerHTML = `
    <div class="wr-refresh-notice" style="display:flex">
      <div class="wr-refresh-copy">
        <div class="wr-refresh-title">${escapeHtml(labels.diarySnapshotMissingTitle)}</div>
        <div class="wr-refresh-desc">${escapeHtml(labels.diarySnapshotMissingDesc)}</div>
      </div>
      <button class="wr-export-btn wr-refresh-action" id="${buttonId}" type="button">${escapeHtml(labels.rebuildData)}</button>
    </div>`;
  const btn = document.getElementById(buttonId);
  if (btn) {
    btn.onclick = () => refreshPeriodAssets(fullDate, 1, buttonId, async () => {
      await showDiaryByDate(fullDate, navEl);
    });
  }
}

// 工具函数：生成 agent 卡片 HTML
function agentCard(name, entries, color) {
  const label = name.includes('(') ? name.split('(')[0].trim() : name;
  const subLabel = name.includes('(') ? name.match(/\(([^)]+)\)/)[1] : '';
  const text = (entries || []).join('；');
  return `<div style="border-left:3px solid ${color};background:var(--light-bg);border-radius:6px;padding:10px 14px;margin-bottom:8px">
    <div style="font-weight:600;color:var(--text);margin-bottom:4px">${label}${subLabel ? ` <span style="font-weight:400;color:var(--gray);font-size:12px">(${subLabel})</span>` : ''}</div>
    <div style="color:var(--text-secondary);font-size:13px;line-height:1.5">${text}</div></div>`;
}

// 工具函数：生成小时热力图
function toggleTask(el, idx) {
  const sub = el.parentElement.querySelector('.task-sub');
  const icon = el.querySelector('.task-icon');
  if (!sub) return;
  const isOpen = sub.style.display !== 'none';
  sub.style.display = isOpen ? 'none' : 'block';
  icon.textContent = isOpen ? '▶' : '▼';
}

function toggleReminder(rid) {
  const body = document.getElementById(rid + '-body');
  const chev = document.getElementById(rid + '-chev');
  if (!body) return;
  if (body.style.maxHeight && body.style.maxHeight !== '0px') {
    body.style.maxHeight = '0px';
    if (chev) chev.style.transform = 'rotate(-90deg)';
  } else {
    body.style.maxHeight = body.scrollHeight + 'px';
    if (chev) chev.style.transform = 'rotate(0deg)';
  }
}

// ── KPI 卡片点击展开面板 ──
function toggleKpiPanel(date, type) {
  const d = (window.__diaryData || {})[date];
  if (!d) return;
  const labels = diaryLabels(d);
  const kpi = d.parsedKpi || {};
  const ct = d.cronTasks || [];
  const fmtT = n => n >= 1e9 ? (n/1e9).toFixed(1)+'B' : n >= 1e6 ? (n/1e6).toFixed(1)+'M' : n >= 1e3 ? (n/1e3).toFixed(1)+'K' : String(n);
  const fmtNumber = n => Number(n || 0).toLocaleString();
  const sourceNote = '<div style="margin-bottom:12px;font-size:12px;color:var(--slate)">' + escapeHtml(labels.foundationSnapshotDetail) + '</div>';
  const agentWork = d.agentWorkNew || {};
  const agentStats = d.agentStats || {};
  const allAgentNames = new Set([...Object.keys(agentWork), ...Object.keys(agentStats)]);
  const agentList = Array.from(allAgentNames).map(name => {
    const tasks = Array.isArray(agentWork[name]) ? agentWork[name] : [];
    const stats = agentStats[name] || {};
    return {
      name,
      taskCount: tasks.length,
      messages: Number(stats.messages || 0),
      tokens: Number(stats.tokens || 0),
      lastActive: stats.lastActive || '',
    };
  }).sort((a, b) => b.taskCount - a.taskCount || b.messages - a.messages || a.name.localeCompare(b.name));
  let title = '';
  let html = '';
  if (type === 'tokens') {
    title = labels.tokenDetails;
    const rows = [
      ['Input Tokens', fmtT(kpi.input_tokens||0)],
      ['Output Tokens', fmtT(kpi.output_tokens||0)],
      ['Cache Read', fmtT(kpi.cache_read||0)],
      ['Cache Write', fmtT(kpi.cache_write||0)],
      ['Total Tokens', '<b style="font-size:16px">' + fmtT(kpi.total_tokens||0) + '</b>'],
      ['API Calls', fmtNumber(kpi.api_calls||0)],
    ];
    const hourly = d.hourlyTokens || {};
    const topHours = Object.entries(hourly)
      .map(([hour, value]) => [hour, Number(value || 0)])
      .filter(([, value]) => value > 0)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6);
    html = sourceNote + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:0;border:1px solid var(--border);border-radius:8px;overflow:hidden">' +
      rows.map(r => '<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border);border-right:1px solid var(--border);background:' + (r[0]==='Total Tokens'?'rgba(83,58,253,0.04)':'var(--white)') + '"><span style="font-size:13px;color:var(--slate)">' + r[0] + '</span><span style="font-size:13px;font-weight:500;color:var(--navy)">' + r[1] + '</span></div>').join('') + '</div>' +
      (topHours.length ? '<div style="margin-top:14px;font-size:11px;font-weight:600;color:var(--slate)">' + escapeHtml(labels.hourlyTokens) + '</div><div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px">' +
        topHours.map(([hour, value]) => '<span class="settings-runtime-chip ok">' + escapeHtml(hour) + ': ' + escapeHtml(fmtT(value)) + '</span>').join('') + '</div>' : '');

  } else if (type === 'cron') {
    title = labels.cronDetails;
    if (ct.length === 0) {
      html = sourceNote + '<div style="color:var(--slate);font-size:14px;padding:16px">' + escapeHtml(labels.noCronData) + '</div>';
    } else {
      const statusText = task => String((task.status || '') + ' ' + (task.note || '') + ' ' + (task.conclusion || '')).toLowerCase();
      const okCount = ct.filter(t => ['✅','成功','正常','完成','done','success','ok'].some(k => statusText(t).includes(k)) && !['失败','错误','error','fail','异常','超时','timeout'].some(k => statusText(t).includes(k))).length;
      const errCount = ct.filter(t => ['❌','失败','错误','error','fail'].some(k => statusText(t).includes(k))).length;
      const warnCount = ct.filter(t => ['⚠️','异常','超时','timeout','warn'].some(k => statusText(t).includes(k))).length;
      const skipCount = ct.filter(t => ['➖','跳过','skip'].some(k => statusText(t).includes(k))).length;
      html = sourceNote + '<div style="margin-bottom:12px;font-size:12px;color:var(--slate)">' + escapeHtml(labels.executionRecordSummary(ct.length, okCount, errCount, warnCount, skipCount)) + '</div>' +
        '<div style="background:var(--white);border:1px solid var(--border);border-radius:8px;overflow:hidden;max-height:60vh;overflow-y:auto"><table style="width:100%;border-collapse:collapse"><tr style="background:rgba(83,58,253,0.06)"><th style="text-align:left;padding:8px 12px;font-size:11px;font-weight:600;color:var(--navy)">' + escapeHtml(labels.task) + '</th><th style="text-align:left;padding:8px 12px;font-size:11px;font-weight:600;color:var(--navy)">' + escapeHtml(labels.executionTime) + '</th><th style="text-align:left;padding:8px 12px;font-size:11px;font-weight:600;color:var(--navy)">' + escapeHtml(labels.status) + '</th></tr>' +
        ct.map(t => {
          const st = statusText(t);
          const sc = ['✅','成功','正常','完成','done','success','ok'].some(k => st.includes(k)) ? 'var(--success)' : ['❌','失败','错误','error','fail'].some(k => st.includes(k)) ? 'var(--ruby)' : ['⚠️','异常','超时','timeout','warn'].some(k => st.includes(k)) ? '#f59e0b' : 'var(--slate)';
          return '<tr style="border-bottom:1px solid var(--border)"><td style="padding:9px 12px;font-size:13px;color:var(--navy);font-weight:500">' + escapeHtml(t.task||t.taskId||'') + '</td><td style="padding:9px 12px;font-size:12px;color:var(--slate);white-space:nowrap">' + escapeHtml(t.time || t.note || t.duration || '') + '</td><td style="padding:9px 12px;font-size:13px;color:' + sc + ';font-weight:500">' + escapeHtml(t.status || t.conclusion || '') + '</td></tr>';
        }).join('') + '</table></div>';
    }
  } else if (type === 'messages') {
    title = labels.sessionDetails;
    const sbs = d.sessionBySource || {};
    const agents = agentList.slice().sort((a,b) => b.messages - a.messages || b.taskCount - a.taskCount);
    if (Object.keys(sbs).length > 0) {
      // Per-source sessions from actanara JSON block
      const sourceLabels = {'openclaw':'OpenClaw','gemini-cli':'Gemini CLI','claude-code':'Claude Code','codex':'Codex','hermes':'Hermes','opencode':'OpenCode','antigravity':'Antigravity','cursor':'Cursor','cron':'Cron'};
      const sourceColors = {'openclaw':'#533afd','gemini-cli':'#2563eb','claude-code':'#dc2626','codex':'#10B981','hermes':'#0891b2','opencode':'#0EA5A4','antigravity':'#6366F1','cursor':'#7C3AED','cron':'#6b7280'};
      const sourceEntries = Object.entries(sbs).sort((a,b) => (b[1].active_sessions||0) - (a[1].active_sessions||0));
      const totalActive = sourceEntries.reduce((s,e) => s + (e[1].active_sessions||0), 0);
      const totalAll = sourceEntries.reduce((s,e) => s + (e[1].sessions_total||0), 0);
      html = sourceNote + '<div style="margin-bottom:12px;font-size:12px;color:var(--slate)">' + escapeHtml(labels.dataSourcesSummary(sourceEntries.length, totalActive, totalAll)) + '</div>' +
        '<div style="background:var(--white);border:1px solid var(--border);border-radius:8px;overflow:hidden"><table style="width:100%;border-collapse:collapse"><tr style="background:rgba(83,58,253,0.06)"><th style="text-align:left;padding:10px 14px;font-size:11px;font-weight:600;color:var(--navy)">' + escapeHtml(labels.dataSource) + '</th><th style="text-align:right;padding:10px 14px;font-size:11px;font-weight:600;color:var(--navy)">' + escapeHtml(labels.activeSessions) + '</th><th style="text-align:right;padding:10px 14px;font-size:11px;font-weight:600;color:var(--navy)">' + escapeHtml(labels.totalSessions) + '</th><th style="text-align:right;padding:10px 14px;font-size:11px;font-weight:600;color:var(--navy)">' + escapeHtml(labels.activeRate) + '</th></tr>' +
        sourceEntries.map(([src, s]) => {
          const active = s.active_sessions || 0;
          const total = s.sessions_total || 0;
          const rate = total > 0 ? Math.round(active/total*100) : 0;
          const color = sourceColors[src] || '#6b7280';
          const rateColor = rate > 30 ? 'var(--success)' : rate > 10 ? '#f59e0b' : 'var(--slate)';
          return '<tr style="border-bottom:1px solid var(--border)"><td style="padding:10px 14px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + color + ';margin-right:8px"></span><span style="font-size:13px;font-weight:500;color:var(--navy)">' + (sourceLabels[src]||src) + '</span></td><td style="text-align:right;padding:10px 14px;font-size:13px;font-weight:600;color:var(--navy)">' + active + '</td><td style="text-align:right;padding:10px 14px;font-size:13px;color:var(--text-secondary)">' + total + '</td><td style="text-align:right;padding:10px 14px;font-size:12px;color:' + rateColor + ';font-weight:500">' + rate + '%</td></tr>';
        }).join('') + '</table></div>' +
        (agents.length > 0 ? '<div style="margin-top:16px;margin-bottom:8px;font-size:11px;font-weight:600;color:var(--slate)">' + escapeHtml(labels.agentMessageDistribution) + '</div><div style="background:var(--white);border:1px solid var(--border);border-radius:8px;overflow:hidden">' +
        agents.map(row => '<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border)"><span style="font-size:13px;font-weight:500;color:var(--navy)">' + escapeHtml(row.name) + '</span><span style="font-size:12px;color:var(--text-secondary)"><b>' + fmtNumber(row.messages) + '</b> msgs · ' + fmtT(row.tokens) + ' tokens · ' + fmtNumber(row.taskCount) + ' tasks</span></div>').join('') + '</div>' : '');
    } else if (agents.length > 0) {
      const totalMsg = Number(kpi.messages_count || agents.reduce((s,a) => s + a.messages, 0));
      const totalTok = Number(kpi.total_tokens || agents.reduce((s,a) => s + a.tokens, 0));
      html = sourceNote + '<div style="margin-bottom:12px;font-size:12px;color:var(--slate)">' + escapeHtml(labels.agentMessageSummary(agents.length, totalMsg.toLocaleString(), fmtT(totalTok))) + '</div>' +
        '<div style="background:var(--white);border:1px solid var(--border);border-radius:8px;overflow:hidden;max-height:60vh;overflow-y:auto">' +
        agents.map(row => '<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border)"><div><span style="font-size:14px;font-weight:500;color:var(--navy)">' + escapeHtml(row.name) + '</span><span style="font-size:12px;color:var(--slate);margin-left:10px">' + fmtNumber(row.taskCount) + ' tasks</span></div><div style="font-size:13px;color:var(--text-secondary)"><b>' + fmtNumber(row.messages) + '</b> msgs · ' + fmtT(row.tokens) + ' tokens</div></div>').join('') + '</div>';
    } else {
      html = sourceNote + '<div style="color:var(--slate);font-size:14px;padding:16px">' + escapeHtml(labels.noSessionData) + '</div>';
    }
  } else if (type === 'agents') {
    title = labels.agentDetails;
    if (agentList.length === 0) {
      html = sourceNote + '<div style="color:var(--slate);font-size:14px;padding:16px">' + escapeHtml(labels.noAgentData) + '</div>';
    } else {
      const activeCount = agentList.filter(row => row.taskCount > 0 || row.messages > 0).length;
      html = sourceNote + '<div style="margin-bottom:12px;font-size:12px;color:var(--slate)">' + escapeHtml(labels.activeAgentSummary(agentList.length, activeCount)) + '</div>' +
        '<div style="background:var(--white);border:1px solid var(--border);border-radius:8px;overflow:hidden;max-height:60vh;overflow-y:auto">' +
        agentList.map(row => {
          const active = row.taskCount > 0 || row.messages > 0;
          const meta = row.taskCount + ' tasks' + (row.messages ? ' · ' + fmtNumber(row.messages) + ' msgs' : '') + (row.lastActive ? ' · ' + labels.lastActivePrefix + row.lastActive.replace('T',' ') : '');
          return '<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border)"><div style="display:flex;align-items:center;gap:10px"><span style="width:9px;height:9px;border-radius:50%;background:' + (active ? 'var(--success)' : 'var(--border)') + ';flex-shrink:0"></span><span style="font-size:14px;font-weight:500;color:' + (active ? 'var(--navy)' : 'var(--slate)') + '">' + escapeHtml(row.name) + '</span></div><div style="font-size:12px;color:var(--slate)">' + escapeHtml(meta || labels.inactive) + '</div></div>';
        }).join('') + '</div>';
    }
  }
  openModal(title, '<div style="padding:8px 4px">' + html + '</div>');
}


function hourlyHeatmap(hourlyTokens, maxVal) {
  const labels = dashboardLanguageProfile() === 'en' ? DIARY_LABELS.en : DIARY_LABELS.zh;
  const bins = Array.from({length: 24}, (_, idx) => {
    const hour = (idx + 4) % 24;
    return {label: String(hour).padStart(2, '0'), hour};
  });
  const hv = hourlyTokens || {};
  const max = maxVal || Math.max(...bins.map(b => Number(hv[b.label] ?? hv[b.hour] ?? 0)), 1);
  const fmt = (v) => v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1000 ? (v/1000).toFixed(0)+'K' : v;
  return `<div style="display:grid;grid-template-columns:repeat(24,1fr);gap:2px;margin-bottom:6px">
    ${bins.map((b) => {
      const v = Number(hv[b.label] ?? hv[b.hour] ?? 0);
      const ratio = max > 0 ? v / max : 0;
      const bg = ratio > 0.7 ? '#4c1d95' : ratio > 0.4 ? '#7c3aed' : ratio > 0.15 ? '#a78bfa' : ratio > 0 ? '#c4b5fd' : '#ede9fe';
      const color = ratio > 0.4 ? 'white' : 'var(--text)';
      const label = fmt(v);
      return `<div style="background:${bg};width:100%;height:48px;border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:9px;color:${color};font-weight:${ratio>0.4?'bold':'normal'}" title="${b.label}: ${label}">${ratio > 0 ? b.label : ''}</div>`;
    }).join('')}
  </div>
  <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--gray)"><span>💜 ${escapeHtml(labels.heatmapLegend)}</span><span>${escapeHtml(labels.peak)} ${fmt(max)}</span></div>`;
}

const DIARY_LABELS = {
  zh: {
    emptyEyebrow: '空白日记',
    emptyTitle: '今日无活动',
    emptyCopy: '当天未检测到有效交互活动，仅保留日期、天气与定时任务记录。',
    scheduledJobs: '定时任务',
    noScheduledJobs: '无定时任务记录',
    scheduledJobFallback: '定时任务',
    activeAgents: '活跃 Agent',
    messages: '总消息数',
    activeSessions: '活跃 Sessions',
    tokenUsage: 'Token 消耗',
    cacheHit: '缓存命中',
    details: '详情',
    successRate: '成功率',
    noData: '无数据',
    summary: '今日概要',
    itemCount: '项',
    agentWork: 'Agent 工作',
    hourlyTokens: 'Token 小时分布',
    reminders: '重要提醒',
    reminderFallback: '提醒',
    lessons: '今日黄金教训',
    recommendation: '建议',
    infrastructure: '基础设施变动',
    target: '对象',
    change: '变动描述',
    current: '当前值',
    notes: '备注',
    rawDiary: '日记原文',
    expand: '点击展开',
    expanded: '已展开',
    tokenDetails: 'Token 消耗详情',
    cronDetails: '定时任务详情',
    sessionDetails: 'Session 统计详情',
    agentDetails: '活跃 Agent 详情',
    foundationSnapshotDetail: '详情与顶部卡片使用同一份 Foundation 日记页面投影；不再回读旧 JSONL / cron 扫描来源。',
    metric: '指标',
    task: '任务',
    executionTime: '执行时间',
    status: '状态',
    noCronData: '无定时任务数据',
    executionRecordSummary: (total, ok, failed, timeout, skipped) => `共 ${total} 条执行记录 · 成功 ${ok} · 失败 ${failed} · 超时 ${timeout} · 跳过 ${skipped}`,
    dataSourcesSummary: (sources, active, total) => `共 ${sources} 个数据源 · ${active} 活跃 / ${total} 总计`,
    dataSource: '数据源',
    totalSessions: '总 Sessions',
    activeRate: '活跃率',
    agentMessageDistribution: 'Agent 消息分布',
    noSessionData: '无 session 数据',
    agentMessageSummary: (agents, messages, tokens) => `共 ${agents} 个 Agent · ${messages} 条消息 · ${tokens} Tokens`,
    noAgentData: '无 agent 数据',
    activeAgentSummary: (agents, active) => `共 ${agents} 个 Agent，其中 ${active} 个有消息记录`,
    lastActivePrefix: '最后活跃 ',
    inactive: '未活跃',
    heatmapLegend: '颜色越深 = Token 越多',
    peak: '峰值',
    more: '等',
  },
  en: {
    emptyEyebrow: 'Blank Diary',
    emptyTitle: 'No Activity Today',
    emptyCopy: 'No meaningful interaction was detected for this day; only date, weather, and scheduled job records are retained.',
    scheduledJobs: 'Scheduled Jobs',
    noScheduledJobs: 'No scheduled job records',
    scheduledJobFallback: 'Scheduled job',
    activeAgents: 'Active Agents',
    messages: 'Messages',
    activeSessions: 'active sessions',
    tokenUsage: 'Token Usage',
    cacheHit: 'cache hit',
    details: 'Details',
    successRate: 'success rate',
    noData: 'No data',
    summary: 'Daily Overview',
    itemCount: 'items',
    agentWork: 'Agent Work',
    hourlyTokens: 'Hourly Tokens',
    reminders: 'Important Notices',
    reminderFallback: 'Notice',
    lessons: 'Lessons',
    recommendation: 'Recommendation',
    infrastructure: 'Infrastructure Updates',
    target: 'Object',
    change: 'Change',
    current: 'Current Value',
    notes: 'Notes',
    rawDiary: 'Raw Diary',
    expand: 'Click to expand',
    expanded: 'Expanded',
    tokenDetails: 'Token Usage Details',
    cronDetails: 'Scheduled Job Details',
    sessionDetails: 'Session Statistics Details',
    agentDetails: 'Active Agent Details',
    foundationSnapshotDetail: 'Details use the same Foundation diary-page projection as the top cards; legacy JSONL / cron scans are not reread.',
    metric: 'Metric',
    task: 'Task',
    executionTime: 'Execution Time',
    status: 'Status',
    noCronData: 'No scheduled job data',
    executionRecordSummary: (total, ok, failed, timeout, skipped) => `${total} execution records · ${ok} succeeded · ${failed} failed · ${timeout} timed out · ${skipped} skipped`,
    dataSourcesSummary: (sources, active, total) => `${sources} data sources · ${active} active / ${total} total`,
    dataSource: 'Data Source',
    totalSessions: 'Total Sessions',
    activeRate: 'Active Rate',
    agentMessageDistribution: 'Agent Message Distribution',
    noSessionData: 'No session data',
    agentMessageSummary: (agents, messages, tokens) => `${agents} Agents · ${messages} messages · ${tokens} Tokens`,
    noAgentData: 'No agent data',
    activeAgentSummary: (agents, active) => `${agents} Agents · ${active} with messages`,
    lastActivePrefix: 'Last active ',
    inactive: 'Inactive',
    heatmapLegend: 'Darker color = more tokens',
    peak: 'Peak',
    more: 'more',
  }
};

function diaryLabels(d) {
  const profile = String((d && d.languageProfile) || '').toLowerCase();
  return profile.startsWith('en') ? DIARY_LABELS.en : DIARY_LABELS.zh;
}

// 主渲染函数
function renderDiaryContent(shortDate, d) {
  const container = document.getElementById('diary-content-' + shortDate);
  if (!container) return;
  const labels = diaryLabels(d);

  const fmtTokens = (v) => { v = Number(v)||0; return v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1000 ? (v/1000).toFixed(0)+'K' : String(v); };
  const escapeHtml = (text) => { const div = document.createElement('div'); div.textContent = text; return div.innerHTML; };
  const renderMd = (text) => escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code style="background:rgba(0,0,0,0.04);padding:1px 5px;border-radius:3px;font-family:var(--font-mono);font-size:12px">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  const periodClass = (period) => {
    const value = String(period || '').toLowerCase();
    if (value.includes('上午') || value.includes('morning')) return 'period-morning';
    if (value.includes('下午') || value.includes('afternoon')) return 'period-afternoon';
    if (value.includes('深夜') || value.includes('late')) return 'period-late-night';
    if (value.includes('凌晨') || value.includes('early')) return 'period-dawn';
    return 'period-default';
  };

  // ── 数据全部来自日记.md（parsedKpi）──
  const kpi = d.parsedKpi || {};
  const agentWorkNew = d.agentWorkNew || {};
  const agentKeys = Object.keys(agentWorkNew);
  const cronTasks = d.cronTasks || [];
  const reminders = d.reminders || [];
  const notes = d.notes || [];
  const hourlyTokens = d.hourlyTokens || {};
  const maxHourly = Math.max(...Object.values(hourlyTokens).map(Number), 1);

  // ── 天气移到页面 header ──
  const weatherEl = document.getElementById('diary-weather-' + shortDate);
  if (weatherEl && d.weather) {
    const weatherText = d.weather.replace(/\(工具获取\)/, '').trim();
    weatherEl.innerHTML = `<span style="margin-right:12px">🌤️ ${escapeHtml(weatherText)}</span>`;
  }

  if (d.activityState === 'empty') {
    const cronRows = cronTasks.length
      ? cronTasks.map(t => `<div class="blank-diary-cron-row"><span>${escapeHtml(t.time || '—')}</span><b>${escapeHtml(t.task || t.taskId || labels.scheduledJobFallback)}</b><em>${escapeHtml(t.status || t.note || '')}</em></div>`).join('')
      : `<div class="blank-diary-muted">${escapeHtml(labels.noScheduledJobs)}</div>`;
    container.innerHTML = `<div class="diary-content-stack blank-diary-stack">
      <div class="section blank-diary-hero">
        <div class="blank-diary-eyebrow">${escapeHtml(labels.emptyEyebrow)}</div>
        <div class="blank-diary-title">${escapeHtml(labels.emptyTitle)}</div>
        <div class="blank-diary-copy">${escapeHtml(labels.emptyCopy)}</div>
      </div>
      <div class="section blank-diary-cron">
        <div class="section-title"><span class="section-title-num">01</span> ${escapeHtml(labels.scheduledJobs)}</div>
        <div class="blank-diary-cron-list">${cronRows}</div>
      </div>
    </div>`;
    return;
  }

  // Section 编号
  let secNum = 0;
  const sec = () => String(++secNum).padStart(2, '0');

  // ── 1. KPI 卡片（数据来自 parsedKpi / 本日统计）──
  const cronOk = cronTasks.filter(t => {
    const s = (t.status||'') + (t.note||'');
    const okKeywords = ['✅','成功','正常','完成','done','success','ok'];
    const failKeywords = ['失败','错误','error','fail','异常','超时','timeout'];
    const hasOk = okKeywords.some(k => s.toLowerCase().includes(k));
    const hasFail = failKeywords.some(k => s.toLowerCase().includes(k));
    return hasOk && !hasFail;
  }).length;
  const cronValid = cronTasks.filter(t => t.status && !(t.status||'').includes('🗑') && !(t.status||'').includes('unload'));
  const cronTotal = cronValid.length;
  const totalMsg = kpi.messages_count || 0;
  const activeSess = kpi.active_sessions || 0;
  const totalSess = kpi.sessions_total || kpi.sessions_count || 0;
  const totalTok = kpi.total_tokens || 0;
  const cacheRate = kpi.cache_hit_rate;

  // 缓存当前日记数据供 KPI 面板使用
  window.__diaryData = window.__diaryData || {};
  window.__diaryData[shortDate] = d;
  const kpiCards = `<div class="stat-grid">
    <div class="stat-card" style="cursor:pointer" onclick="toggleKpiPanel('${shortDate}','agents')"><div class="stat-card-label">🤖 ${escapeHtml(labels.activeAgents)}</div><div class="stat-card-value">${agentKeys.length}</div><div class="stat-card-trend trend-neutral">${agentKeys.slice(0,4).join(' · ')}${agentKeys.length>4?' · '+escapeHtml(labels.more):''}<span style="margin-left:auto;font-size:11px;color:#3b5998">${escapeHtml(labels.details)} →</span></div></div>
    <div class="stat-card" style="cursor:pointer" onclick="toggleKpiPanel('${shortDate}','messages')"><div class="stat-card-label">💬 ${escapeHtml(labels.messages)}</div><div class="stat-card-value">${totalMsg.toLocaleString()}</div><div class="stat-card-trend trend-neutral">${activeSess}/${totalSess} ${escapeHtml(labels.activeSessions)}<span style="margin-left:auto;font-size:11px;color:#3b5998">${escapeHtml(labels.details)} →</span></div></div>
    <div class="stat-card" style="cursor:pointer" onclick="toggleKpiPanel('${shortDate}','tokens')"><div class="stat-card-label">⏱️ ${escapeHtml(labels.tokenUsage)}</div><div class="stat-card-value">${fmtTokens(totalTok)}</div><div class="stat-card-trend trend-neutral">${cacheRate ? cacheRate+'% '+escapeHtml(labels.cacheHit) : ''}<span style="margin-left:auto;font-size:11px;color:#3b5998">${escapeHtml(labels.details)} →</span></div></div>
    <div class="stat-card" style="cursor:pointer" onclick="toggleKpiPanel('${shortDate}','cron')"><div class="stat-card-label">⏰ ${escapeHtml(labels.scheduledJobs)}</div><div class="stat-card-value">${cronOk}/${cronTotal}</div><div class="stat-card-trend trend-neutral">${cronTotal>0?Math.round(cronOk/cronTotal*100)+'% '+labels.successRate:labels.noData}<span style="margin-left:auto;font-size:11px;color:#3b5998">${escapeHtml(labels.details)} →</span></div></div>
  </div>`;

  // ── 2. 今日概要（* 主条目 → - 子条目，点击展开）──
  const summaryTopics = d.summaryTopics || [];
  let summaryHtml = '';
  if (summaryTopics.length > 0) {
    const cards = summaryTopics.map((topic, idx) => {
      const items = topic.items || [];
      const hasItems = items.length > 0;
      const uid = 'summary-' + shortDate + '-' + idx;
      const itemsHtml = items.map(item =>
        `<div class="diary-summary-subitem">${renderMd(item)}</div>`
      ).join('');
      return `<div class="diary-summary-card">
        <div class="diary-summary-head" onclick="const sub=document.getElementById('${uid}');if(!sub)return;const open=sub.style.display!=='none';sub.style.display=open?'none':'block';const arrow=this.querySelector('.sa');if(arrow)arrow.style.transform=open?'':'rotate(90deg)'">
          ${hasItems ? `<span class="sa diary-summary-arrow">▶</span>` : `<span class="diary-summary-dot">●</span>`}
          <span class="diary-summary-index">${String(idx + 1).padStart(2, '0')}</span>
          <span class="diary-summary-title">${renderMd(topic.title)}</span>
          ${hasItems ? `<span class="diary-summary-count">${items.length} ${escapeHtml(labels.itemCount)}</span>` : ''}
        </div>
        ${hasItems ? `<div id="${uid}" class="diary-summary-body" style="display:none">${itemsHtml}</div>` : ''}
      </div>`;
    }).join('');
    summaryHtml = `<div class="section"><div class="section-title"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.summary)}</div>${cards}</div>`;
  } else if (d.summary) {
    summaryHtml = `<div class="section"><div class="section-title"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.summary)}</div><div class="narrative">${renderMd(d.summary)}</div></div>`;
  }

  // ── 3. Agent 工作（保持不变）──
  let agentHtml = '';
  if (agentKeys.length > 0) {
    const panels = agentKeys.map(k => {
      const tasks = agentWorkNew[k] || [];
      const taskItems = tasks.map((task, ti) => {
        const subs = (task.sub_items || []).map((si, sii) => {
          const isDict = typeof si === 'object' && si !== null;
          const text = isDict ? si.text : si;
          const details = isDict && si.details && si.details.length
            ? '<div class="diary-subitem-details">' + si.details.map(d => '<div>' + renderMd(d) + '</div>').join('') + '</div>'
            : '';
          return '<div class="diary-task-subitem"><span>' + String(sii + 1).padStart(2, '0') + '</span><div>' + renderMd(text) + details + '</div></div>';
        }).join('');
        const hasSubs = subs.length > 0;
        return `<div class="diary-task-block"><div class="task-row diary-task-row" style="cursor:${hasSubs?'pointer':'default'}" onclick="${hasSubs?`toggleTask(this,${ti})`:''}"><span class="task-icon">${hasSubs?'▼':'▷'}</span><span class="diary-period-pill ${periodClass(task.period)}">${escapeHtml(task.period)}</span><span class="diary-task-title">${renderMd(task.main_task)}</span>${hasSubs?`<span class="diary-task-count">(${task.sub_items.length})</span>`:''}</div>${hasSubs?`<div class="task-sub diary-task-sub">${subs}</div>`:''}</div>`;
      }).join('');
      return `<div class="diary-agent-panel"><div class="diary-agent-title">${escapeHtml(k)} <span>${tasks.length} ${escapeHtml(labels.itemCount)}</span></div>${taskItems}</div>`;
    }).join('');
    agentHtml = `<div class="section"><div class="section-title"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.agentWork)}</div>${panels}</div>`;
  }

  // ── 4. Token 小时分布 ──
  const heatmap = hourlyHeatmap(hourlyTokens, maxHourly);
  const heatmapHtml = `<div class="section"><div class="section-title"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.hourlyTokens)}</div>${heatmap}</div>`;

  // ── 5. 重要提醒 ──
  let remindersHtml = '';
  if (reminders.length > 0) {
    const rCards = reminders.map((r, ri) => {
      const items = r.items || [];
      const multiItem = items.length >= 1;
      const rid = `rem-${shortDate}-${ri}`;
      let bodyHtml = '';
      if (items.length === 1) {
        bodyHtml = renderMd(items[0]);
      } else if (multiItem) {
        bodyHtml = '<div class="diary-reminder-list">' + items.map(it =>
          `<div class="diary-reminder-item">${renderMd(it)}</div>`
        ).join('') + '</div>';
      } else if (r.desc) {
        bodyHtml = renderMd(r.desc);
      }
      const chevron = multiItem ? `<span id="${rid}-chev" class="diary-reminder-chev">▼</span>` : '';
      const bodyStyle = multiItem ? `id="${rid}-body" style="max-height:0;overflow:hidden;transition:max-height 0.3s ease"` : '';
      const click = multiItem ? ` onclick="toggleReminder('${rid}')" style="cursor:pointer"` : '';
      return `<div class="diary-reminder-card"><div${click} class="diary-reminder-title">${renderMd(r.title || labels.reminderFallback)}${chevron}</div><div ${bodyStyle}>${bodyHtml}</div></div>`;
    }).join('');
    remindersHtml = `<div class="section"><div class="section-title"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.reminders)}</div>${rCards}</div>`;
  }

  // ── 6. 今日黄金教训 ──
  let lessonsHtml = '';
  if (d.lessons && d.lessons.length > 0) {
    const cards = d.lessons.map((l, i) => {
      return `<details class="diary-lesson-card">
        <summary><span class="diary-lesson-agent">${escapeHtml(l.agent)}</span><span class="diary-lesson-problem">${renderMd(l.problem)}</span></summary>
        <div class="diary-lesson-suggestion"><span>${escapeHtml(labels.recommendation)}</span>${renderMd(l.suggestion)}</div>
      </details>`;
    }).join('');
    lessonsHtml = `<div class="section"><div class="section-title"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.lessons)}</div>${cards}</div>`;
  }

  // ── 7. 基础设施变动 ──
  let infraHtml = '';
  if (d.infraChanges && d.infraChanges.length > 0) {
    const rows = d.infraChanges.map(ic =>
      `<tr>
        <td class="diary-infra-target">${escapeHtml(ic.target)}</td>
        <td>${renderMd(ic.change)}</td>
        <td class="diary-infra-current">${escapeHtml(ic.current)}</td>
      </tr>`
    ).join('');
    infraHtml = `<div class="section"><div class="section-title"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.infrastructure)}</div>
      <div class="diary-infra-shell">
        <table class="diary-infra-table">
          <tr>
            <th>${escapeHtml(labels.target)}</th>
            <th>${escapeHtml(labels.change)}</th>
            <th>${escapeHtml(labels.current)}</th>
          </tr>
          ${rows}
        </table>
      </div>
    </div>`;
  }

  // ── 8. 备注（结构化，类似重要提醒）──
  let notesHtml = '';
  if (notes.length > 0) {
    const noteIcons = ['📝','📌','🔧','💡','📊','📋'];
    const nCards = notes.map((n, ni) => {
      const icon = noteIcons[ni % noteIcons.length];
      const title = n.title || labels.notes;
      const items = (n.items || []);
      const itemsHtml = items.map(item =>
        `<div class="diary-note-item">${renderMd(item)}</div>`
      ).join('');
      return `<div class="diary-note-card">
        <div class="diary-note-title"><span>${escapeHtml(icon)}</span>${escapeHtml(title)}</div>
        ${itemsHtml}
      </div>`;
    }).join('');
    notesHtml = `<div class="section"><div class="section-title"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.notes)}</div>${nCards}</div>`;
  }

  // ── 8. 日记原文（默认折叠）──
  let rawHtml = '';
  if (d.rawContent) {
    const rawUid = 'raw-' + shortDate;
    rawHtml = `<div class="section"><div class="section-title diary-raw-title" onclick="const el=document.getElementById('${rawUid}');if(!el)return;const open=el.style.display!=='none';el.style.display=open?'none':'block';this.querySelector('.raw-arrow').textContent=open?'▸':'▾';this.querySelector('.diary-raw-hint').textContent=open?'${escapeHtml(labels.expand)}':'${escapeHtml(labels.expanded)}'"><span class="section-title-num">${sec()}</span> ${escapeHtml(labels.rawDiary)} <span class="raw-arrow">▸</span><span class="diary-raw-hint">${escapeHtml(labels.expand)}</span></div><div id="${rawUid}" style="display:none"><div class="report-body">${marked.parse(d.rawContent)}</div></div></div>`;
  }

  container.innerHTML = `<div class="diary-content-stack">${kpiCards}${summaryHtml}${agentHtml}${heatmapHtml}${remindersHtml}${lessonsHtml}${infraHtml}${notesHtml}${rawHtml}</div>`;
}

function showPage(id, navEl) {
  // Update page display
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const page = document.getElementById('page-' + id);
  if (page) page.classList.add('active');
  // Update nav active state
  if (navEl) {
    navEl.classList.add('active');
    const dot = navEl.querySelector('.nav-item-dot');
    if (dot) dot.classList.add('active');
  }
  // Update URL hash for browser back/forward support
  location.hash = 'page-' + id;
  if (id === 'static') aaEnsureAssetsLoaded();
  if (id === 'foundation-ops') loadFoundationOps();
  if (id === 'rag-search') loadRagSearchPage();
}

// Restore page from URL hash (called on page load and hashchange)
function showPageFromHash() {
  const hash = location.hash.replace('#', '') || 'page-home';
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const page = document.getElementById(hash);
  if (page) page.classList.add('active');
  // Find and activate the corresponding nav item
  const navHash = hash.replace('page-', '');
  document.querySelectorAll('.nav-item').forEach(n => {
    if (n.getAttribute('onclick')?.includes("'" + navHash + "'")) {
      n.classList.add('active');
      const dot = n.querySelector('.nav-item-dot');
      if (dot) dot.classList.add('active');
    }
  });
  if (hash === 'page-static') aaEnsureAssetsLoaded();
  if (hash === 'page-foundation-ops') loadFoundationOps();
  if (hash === 'page-rag-search') loadRagSearchPage();
}

function openModal(title, content) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = content;
  document.getElementById('modal').classList.add('active');
  document.body.style.overflow = 'hidden';
  modalHistory.push({ title, content });
}

function closeModal() {
  document.getElementById('modal').classList.remove('active');
  document.body.style.overflow = '';
  modalHistory = [];
  if (backgroundTasksTimer) {
    clearInterval(backgroundTasksTimer);
    backgroundTasksTimer = null;
  }
}

function modalBack() {
  if (modalHistory.length > 1) {
    modalHistory.pop();
    const prev = modalHistory[modalHistory.length - 1];
    document.getElementById('modal-title').textContent = prev.title;
    document.getElementById('modal-body').innerHTML = prev.content;
  } else {
    closeModal();
  }
}

function openGithubTodo() {
  const labels = operatorText();
  openModal(labels.githubProject, '<div class="settings-note">' + escapeHtml(labels.githubTodo) + '</div>');
}

function openI18nTodo() {
  const labels = operatorText();
  openModal(labels.i18nSwitch, '<div class="settings-note">' + escapeHtml(labels.i18nTodo) + '</div>');
}

function settingsTab(name) {
  document.querySelectorAll('.settings-tab').forEach(el => el.classList.toggle('active', el.dataset.tab === name));
  document.querySelectorAll('.settings-pane').forEach(el => el.classList.toggle('active', el.dataset.pane === name));
  if (name === 'onboarding') loadOnboardingReadiness();
  if (name === 'workspaceAttribution') loadWorkspaceAttributionSettings();
}

async function openSettingsModal() {
  const labels = operatorText();
  openModal(labels.settingsTitle, '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingSettings) + '</span></div>');
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const settings = await res.json();
    rememberDashboardSettings(settings);
    document.getElementById('modal-body').innerHTML = renderSettingsModal(settings);
    settingsTab('schedule');
  } catch (e) {
    document.getElementById('modal-body').innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.settingsReadFailed + e.message) + '</div>';
  }
}

function renderSettingsModal(settings) {
  const labels = operatorText();
  const schedule = settings.schedule || {};
  const features = settings.features || {};
  const paths = settings.paths || {};
  const authority = settings.authority || {};
  const general = settings.general || (authority.general || {});
  const dashboard = settings.dashboard || (authority.dashboard || {});
  const runtimeSources = settings.runtimeSources || (authority.runtimeSources || {});
  const pipeline = settings.pipeline || (authority.pipeline || {});
  const externalTools = settings.externalTools || {};
  const llmProvider = settings.llmProvider || {};
  return `
    <div class="settings-grid">
      <div class="settings-tabs">
        <button class="settings-tab" data-tab="general" onclick="settingsTab('general')">${escapeHtml(labels.tabGeneral)}</button>
        <button class="settings-tab active" data-tab="schedule" onclick="settingsTab('schedule')">${escapeHtml(labels.tabSchedule)}</button>
        <button class="settings-tab" data-tab="paths" onclick="settingsTab('paths')">${escapeHtml(labels.tabPaths)}</button>
        <button class="settings-tab" data-tab="runtimeSources" onclick="settingsTab('runtimeSources')">${escapeHtml(labels.tabRuntimeSources)}</button>
        <button class="settings-tab" data-tab="workspaceAttribution" onclick="settingsTab('workspaceAttribution')">Workspace 归属</button>
        <button class="settings-tab" data-tab="llm" onclick="settingsTab('llm')">LLM</button>
        <button class="settings-tab" data-tab="pipeline" onclick="settingsTab('pipeline')">Pipeline</button>
        <button class="settings-tab" data-tab="externalTools" onclick="settingsTab('externalTools')">${escapeHtml(labels.tabExternalTools)}</button>
        <button class="settings-tab" data-tab="features" onclick="settingsTab('features')">${escapeHtml(labels.tabFeatures)}</button>
        <button class="settings-tab" data-tab="authority" onclick="settingsTab('authority')">Authority</button>
      </div>
      <div>
        <div class="settings-pane" data-pane="general">${renderGeneralSettings(general, dashboard)}</div>
        <div class="settings-pane active" data-pane="schedule">${renderScheduleSettings(schedule, settings.agentSchedulePrompt || '')}</div>
        <div class="settings-pane" data-pane="paths">${renderPathSettings(paths, settings.runtimePath || {})}</div>
        <div class="settings-pane" data-pane="runtimeSources">${renderRuntimeSourceSettings(runtimeSources)}</div>
        <div class="settings-pane" data-pane="workspaceAttribution">${renderWorkspaceAttributionSettings()}</div>
        <div class="settings-pane" data-pane="llm">${renderLlmProviderSettings(llmProvider, false)}</div>
        <div class="settings-pane" data-pane="pipeline">${renderPipelineSettings(pipeline)}</div>
        <div class="settings-pane" data-pane="externalTools">${renderExternalToolSettings(externalTools)}</div>
        <div class="settings-pane" data-pane="features">${renderFeatureSettings(features)}</div>
        <div class="settings-pane" data-pane="authority">${renderSettingsAuthority((authority || {}).settingsAuthority || {})}</div>
        <div class="settings-actions">
          <span class="settings-status" id="settingsSaveStatus">${escapeHtml(labels.configFile)}${escapeHtml(settings.settingsPath || '')}</span>
          <button class="wr-export-btn" onclick="closeModal()">${escapeHtml(labels.cancel)}</button>
          <button class="wr-export-btn" onclick="saveSettingsModal()">${escapeHtml(labels.saveSettings)}</button>
        </div>
      </div>
    </div>`;
}

const ONBOARDING_PROFILE_OPTIONS = [
  ['actanara', 'Actanara', true, true],
  ['dashboard', 'Dashboard', true],
  ['nova-rag', 'nova-RAG', true],
  ['nova-task', 'Nova-Task', true],
  ['dev-test', 'Dev/Test', false],
];

function renderOnboardingSettings() {
  const labels = operatorText();
  const profiles = ONBOARDING_PROFILE_OPTIONS.map(item => {
    const disabled = item[3] ? 'disabled' : '';
    const checked = item[2] ? 'checked' : '';
    return '<label class="settings-check"><input type="checkbox" data-onboarding-profile="' + escapeHtml(item[0]) + '" ' + checked + ' ' + disabled + ' onchange="loadOnboardingReadiness()"> ' + escapeHtml(item[1]) + '</label>';
  }).join('');
  return '<div class="settings-section">' +
    '<div class="settings-section-title">' + escapeHtml(labels.onboardingTitle) + '</div>' +
    '<div class="settings-note">' + escapeHtml(labels.onboardingNote) + '</div>' +
    '<div class="settings-checks">' + profiles + '</div>' +
    '<div class="settings-timer-actions"><button type="button" class="wr-export-btn" onclick="loadOnboardingReadiness()">' + escapeHtml(labels.refreshOnboarding) + '</button></div>' +
    '</div>' +
    '<div class="settings-section"><div class="settings-section-title">Readiness</div><div id="onboardingStatusPanel" class="settings-runtime-status">' + escapeHtml(labels.notReadYet) + '</div></div>' +
    '<div class="settings-section"><div class="settings-section-title">Plan</div><div id="onboardingPlanPanel" class="settings-runtime-status">' + escapeHtml(labels.notReadYet) + '</div></div>';
}

function selectedOnboardingProfiles() {
  const values = Array.from(document.querySelectorAll('[data-onboarding-profile]'))
    .filter(el => el.checked || el.dataset.onboardingProfile === 'actanara')
    .map(el => el.dataset.onboardingProfile)
    .filter(Boolean);
  return Array.from(new Set(values));
}

function onboardingQueryString() {
  const params = new URLSearchParams();
  selectedOnboardingProfiles().forEach(profile => params.append('profile', profile));
  const text = params.toString();
  return text ? '?' + text : '';
}

async function loadOnboardingReadiness() {
  const labels = operatorText();
  const statusPanel = document.getElementById('onboardingStatusPanel');
  const planPanel = document.getElementById('onboardingPlanPanel');
  if (!statusPanel || !planPanel) return;
  statusPanel.innerHTML = '<div class="wr-loading" style="padding:10px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingOnboardingReadiness) + '</span></div>';
  planPanel.innerHTML = '<div class="wr-loading" style="padding:10px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingOnboardingPlan) + '</span></div>';
  try {
    const query = onboardingQueryString();
    const statusRes = await fetch('/api/onboarding/status' + query);
    if (!statusRes.ok) throw new Error('status HTTP ' + statusRes.status);
    const planRes = await fetch('/api/onboarding/plan' + query);
    if (!planRes.ok) throw new Error('plan HTTP ' + planRes.status);
    statusPanel.innerHTML = renderOnboardingStatus(await statusRes.json());
    planPanel.innerHTML = renderOnboardingPlan(await planRes.json());
  } catch (e) {
    const msg = '<div class="fo-job-error">' + escapeHtml(labels.onboardingReadFailed + e.message) + '</div>';
    statusPanel.innerHTML = msg;
    planPanel.innerHTML = msg;
  }
}

function renderOnboardingStatus(payload) {
  const runtime = payload.runtime || {};
  const readiness = payload.readiness || {};
  const deps = payload.dependencyProfiles || {};
  const dependencyGroups = payload.dependencyGroups || [];
  const requirementSets = payload.requirementSets || [];
  const packagingPlan = payload.packagingPlan || {};
  const requiredInputs = payload.requiredInputs || [];
  const resource = payload.resourceProfile || {};
  const rag = payload.rag || {};
  const scheduler = payload.scheduler || {};
  const checks = (readiness.checks || []).map(item => {
    const cls = item.status === 'ok' ? 'ok' : 'warn';
    return '<span class="settings-runtime-chip ' + cls + '">' + escapeHtml(item.id || '') + '=' + escapeHtml(item.status || '') + '</span>';
  }).join('');
  const depRows = ((deps.profiles || [])).map(profile => '<tr><td>' + escapeHtml(profile.label || profile.id) + '</td><td>' + escapeHtml(profile.status || '') + '</td><td>' + escapeHtml((profile.missingRequired || []).join(', ') || '—') + '</td></tr>').join('');
  const groupRows = dependencyGroups.map(group => '<tr><td>' + escapeHtml(group.label || group.id || '') + '</td><td>' + escapeHtml(String(group.selected)) + '</td><td>' + escapeHtml((group.requirementSets || []).join(', ') || '—') + '</td><td>' + escapeHtml((group.providerInputs || []).join(', ') || '—') + '</td></tr>').join('');
  const requirementRows = requirementSets.map(item => '<tr><td>' + escapeHtml(item.label || item.id || '') + '</td><td>' + escapeHtml(item.profile || '') + '</td><td>' + escapeHtml(item.status || '') + '</td><td>' + escapeHtml((item.pendingInputs || []).join(', ') || (item.missingRequired || []).join(', ') || '—') + '</td></tr>').join('');
  const packagingRows = ((packagingPlan.groups || [])).map(item => '<tr><td>' + escapeHtml(item.label || item.id || '') + '</td><td>' + escapeHtml(String(item.selected)) + '</td><td>' + escapeHtml(item.status || '') + '</td><td>' + escapeHtml(item.currentDetection || '') + '</td><td>' + escapeHtml((item.providerInputs || []).join(', ') || '—') + '</td></tr>').join('');
  const inputRows = requiredInputs.map(item => '<tr><td>' + escapeHtml(item.id || '') + '</td><td>' + escapeHtml(item.profile || '') + '</td><td>' + escapeHtml(item.status || '') + '</td></tr>').join('');
  return '<div class="settings-runtime-line"><b>Status</b> ' + escapeHtml(readiness.status || 'unknown') + '</div>' +
    '<div class="settings-runtime-line"><b>Runtime</b><code>' + escapeHtml(runtime.actanaraHome || '—') + '</code></div>' +
    '<div class="settings-runtime-line"><b>Settings</b><code>' + escapeHtml(runtime.settingsPath || '—') + '</code></div>' +
    '<div class="settings-runtime-flags">' + checks + '</div>' +
    '<div class="settings-runtime-line"><b>Resource</b> dashboard=' + escapeHtml(String(((resource.dashboard || {}).expectedResidentProcesses ?? '—'))) +
      ' · rag=' + escapeHtml(String(((resource.rag || {}).expectedResidentProcesses ?? '—'))) +
      ' · pipeline=' + escapeHtml(String(((resource.pipeline || {}).expectedResidentProcesses ?? '—'))) + '</div>' +
    '<div class="settings-runtime-line"><b>nova-RAG</b> enabled=' + escapeHtml(String(rag.enabled)) + ' · mode=' + escapeHtml(rag.mode || '—') + ' · server=' + escapeHtml(String(rag.serverEnabled)) + '</div>' +
    '<div class="settings-runtime-line"><b>Scheduler</b> provider=' + escapeHtml(scheduler.provider || '—') + ' · supported=' + escapeHtml(String(scheduler.supported)) + ' · registered=' + escapeHtml(String(scheduler.registered)) + '</div>' +
    '<div class="settings-runtime-line"><b>Packaging</b> packageManager=' + escapeHtml(packagingPlan.packageManager || 'undecided') + ' · installsDependencies=' + escapeHtml(String(packagingPlan.installsDependencies)) + ' · schedulerIncluded=' + escapeHtml(String(packagingPlan.schedulerIncluded)) + '</div>' +
    '<div class="aa-table-shell"><table class="data-table settings-onboarding-table"><thead><tr><th>Dependency group</th><th>Selected</th><th>Requirement sets</th><th>Inputs</th></tr></thead><tbody>' + groupRows + '</tbody></table></div>' +
    '<div class="aa-table-shell"><table class="data-table settings-onboarding-table"><thead><tr><th>Requirement set</th><th>Profile</th><th>Status</th><th>Pending/missing</th></tr></thead><tbody>' + requirementRows + '</tbody></table></div>' +
    '<div class="aa-table-shell"><table class="data-table settings-onboarding-table"><thead><tr><th>Packaging group</th><th>Selected</th><th>Status</th><th>Detection</th><th>Inputs</th></tr></thead><tbody>' + packagingRows + '</tbody></table></div>' +
    '<div class="aa-table-shell"><table class="data-table settings-onboarding-table"><thead><tr><th>Profile</th><th>Status</th><th>Missing</th></tr></thead><tbody>' + depRows + '</tbody></table></div>' +
    '<div class="aa-table-shell"><table class="data-table settings-onboarding-table"><thead><tr><th>Required input</th><th>Profile</th><th>Status</th></tr></thead><tbody>' + inputRows + '</tbody></table></div>';
}

function renderOnboardingPlan(payload) {
  const summary = payload.summary || {};
  const scheduler = payload.scheduler || {};
  const dependencyGroups = payload.dependencyGroups || [];
  const requirementSets = payload.requirementSets || [];
  const packagingPlan = payload.packagingPlan || {};
  const requiredInputs = payload.requiredInputs || [];
  const profiles = (payload.selectedProfiles || []).join(', ');
  const selectedGroups = dependencyGroups.filter(group => group.selected).map(group => group.id).join(', ');
  const actions = (payload.actions || []).map(action => {
    const cls = action.mode === 'blocked' ? 'warn' : 'ok';
    return '<div class="settings-runtime-line"><span class="settings-runtime-chip ' + cls + '">' + escapeHtml(action.mode || 'plan') + '</span> <b>' + escapeHtml(action.id || '') + '</b> · ' + escapeHtml(action.description || '') + ' · executesShell=' + escapeHtml(String(action.executesShell)) + '</div>';
  }).join('');
  return '<div class="settings-runtime-line"><b>Plan</b> ' + escapeHtml(summary.status || 'unknown') + ' · profiles=' + escapeHtml(profiles || '—') + '</div>' +
    '<div class="settings-runtime-line"><b>Dependencies</b> groups=' + escapeHtml(selectedGroups || '—') + ' · requirementSets=' + escapeHtml(String(summary.requirementSets || requirementSets.filter(item => item.selected).length || 0)) + ' · packagingGroups=' + escapeHtml(String(summary.packagingGroups || ((packagingPlan.summary || {}).groups) || 0)) + ' · missingRequired=' + escapeHtml(String(summary.missingRequired || 0)) + ' · actions=' + escapeHtml(String(summary.actions || 0)) + ' · pendingInputs=' + escapeHtml(String(summary.pendingRequiredInputs || requiredInputs.length || 0)) + '</div>' +
    '<div class="settings-runtime-line"><b>Scheduler</b> model=' + escapeHtml(scheduler.selectionModel || '—') + ' · provider=' + escapeHtml(scheduler.provider || '—') + ' · registrationImplemented=' + escapeHtml(String(scheduler.registrationImplemented)) + '</div>' +
    actions;
}

function renderSettingsAuthority(authority) {
  const labels = operatorText();
  const groups = authority.groups || [];
  if (!groups.length) return '<div class="settings-note">' + escapeHtml(labels.noSettingsAuthority) + '</div>';
  const policy = authority.policy || {};
  const policyRows = [
    [labels.persistentWrite, policy.singleWriter || '$ACTANARA_HOME/config/settings.json'],
    [labels.envSemantics, policy.envSemantics || 'process-local override'],
    [labels.defaultManual, policy.manualVsDefault || 'manual choices must be explicit'],
    [labels.secret, policy.secretHandling || 'redacted in read APIs'],
  ].map(item => '<div class="settings-authority-policy-row"><b>' + escapeHtml(item[0]) + '</b><span>' + escapeHtml(item[1]) + '</span></div>').join('');
  return '<div class="settings-section">' +
    '<div class="settings-section-title">' + escapeHtml(labels.settingsAuthorityTitle) + '</div>' +
    '<div class="settings-note">' + escapeHtml(labels.settingsAuthorityNote) + '</div>' +
    '<div class="settings-runtime-status">' +
      '<div class="settings-runtime-line"><b>Settings</b> <code>' + escapeHtml(authority.settingsPath || '—') + '</code></div>' +
      '<div class="settings-runtime-line"><b>Precedence</b> ' + escapeHtml((authority.precedence || []).join(' > ')) + '</div>' +
      '<div class="settings-authority-policy">' + policyRows + '</div>' +
    '</div>' +
    groups.map(renderSettingsAuthorityGroup).join('') +
    '</div>';
}

function renderSettingsAuthorityGroup(group) {
  const labels = operatorText();
  const fields = group.fields || [];
  const rows = fields.map(field => {
    const flags = [
      field.source ? ('source=' + field.source) : '',
      field.envOverride ? 'env override' : '',
      field.mode ? ('mode=' + field.mode) : '',
      field.drift ? 'drift' : '',
    ].filter(Boolean).map(flag => '<span class="settings-runtime-chip ' + (flag === 'drift' || flag === 'env override' ? 'warn' : 'ok') + '">' + escapeHtml(flag) + '</span>').join('');
    const settingsValue = formatSettingsAuthorityValue(field.settingsValue);
    const effectiveValue = formatSettingsAuthorityValue(field.effectiveValue);
    const auto = field.autoValue === undefined || field.autoValue === null ? '' : '<small>auto=' + escapeHtml(String(field.autoValue)) + '</small>';
    return '<tr>' +
      '<td><code>' + escapeHtml(field.path || '') + '</code></td>' +
      '<td>' + escapeHtml(field.env || '—') + '</td>' +
      '<td>' + escapeHtml(field.defaultSource || '—') + '</td>' +
      '<td>' + settingsValue + '</td>' +
      '<td>' + effectiveValue + auto + '</td>' +
      '<td><div class="settings-authority-flags">' + flags + '</div></td>' +
      '</tr>';
  }).join('');
  return '<div class="settings-authority-group">' +
    '<div class="settings-authority-group-head">' +
      '<b>' + escapeHtml(group.group || 'group') + '</b>' +
      '<span>' + escapeHtml(group.authority || '') + '</span>' +
    '</div>' +
    '<div class="settings-note">' + escapeHtml(group.manualDefaultPolicy || '') + '</div>' +
    '<div class="settings-note">' + escapeHtml(labels.writableVia) + escapeHtml(group.writableVia || '—') + '</div>' +
    '<div class="aa-table-shell"><table class="data-table settings-authority-table"><thead><tr><th>Path</th><th>Env</th><th>Default</th><th>Settings</th><th>Effective</th><th>Source</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
    '</div>';
}

function formatSettingsAuthorityValue(value) {
  if (value === null || value === undefined || value === '') return '<span class="muted">—</span>';
  if (Array.isArray(value)) return '<code>' + escapeHtml(value.join(', ')) + '</code>';
  if (typeof value === 'object') return '<code>' + escapeHtml(JSON.stringify(value)) + '</code>';
  return '<code>' + escapeHtml(String(value)) + '</code>';
}

function renderGeneralSettings(general, dashboard) {
  const labels = operatorText();
  const restartAttr = value => ' data-original-value="' + escapeHtml(value || '') + '" data-requires-restart="dashboard"';
  return `
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.productLocalization)}</div>
      <div class="settings-row"><label>App name</label><input id="setGeneralAppName" value="${escapeHtml(general.appName || 'Actanara')}"></div>
      <div class="settings-row"><label>Environment</label><input id="setGeneralEnvironment" value="${escapeHtml(general.environment || 'local')}"></div>
      <div class="settings-row"><label>Timezone</label><input id="setGeneralTimezone" value="${escapeHtml(general.timezone || 'Asia/Hong_Kong')}"></div>
      <div class="settings-row"><label>Locale</label><input id="setGeneralLocale" value="${escapeHtml(general.locale || 'zh-CN')}"></div>
      <div class="settings-row"><label>Workspace root</label><input id="setGeneralWorkspaceRoot" value="${escapeHtml(general.workspaceRoot || '')}"></div>
      <div class="settings-row"><label>Tmp workspace</label><input id="setGeneralTmpWorkspace" value="${escapeHtml(general.tmpWorkspace || '')}"></div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.dashboardService)}</div>
      <div class="settings-row"><label>Project root</label><input id="setDashboardProjectRoot"${restartAttr(dashboard.projectRoot || '')} value="${escapeHtml(dashboard.projectRoot || '')}"></div>
      <div class="settings-row"><label>Python</label><input id="setDashboardPython"${restartAttr(dashboard.pythonExecutable || 'python3')} value="${escapeHtml(dashboard.pythonExecutable || 'python3')}"></div>
      <div class="settings-row"><label>App dir</label><input id="setDashboardAppDir"${restartAttr(dashboard.appDir || '')} value="${escapeHtml(dashboard.appDir || '')}"></div>
      <div class="settings-row"><label>Host</label><input id="setDashboardHost"${restartAttr(dashboard.host || '127.0.0.1')} value="${escapeHtml(dashboard.host || '127.0.0.1')}"></div>
      <div class="settings-row"><label>Port</label><input id="setDashboardPort" type="number" min="1" max="65535"${restartAttr(dashboard.port || 3036)} value="${escapeHtml(dashboard.port || 3036)}"></div>
      <div class="settings-row"><label>Health path</label><input id="setDashboardHealthPath"${restartAttr(dashboard.healthPath || '/health')} value="${escapeHtml(dashboard.healthPath || '/health')}"></div>
      <div class="settings-row"><label>Logs dir</label><input id="setDashboardLogsDir"${restartAttr(dashboard.logsDir || '')} value="${escapeHtml(dashboard.logsDir || '')}"></div>
      <div class="settings-row"><label>Service label</label><input id="setDashboardServiceLabel"${restartAttr(dashboard.serviceLabel || 'com.actanara.dashboard')} value="${escapeHtml(dashboard.serviceLabel || 'com.actanara.dashboard')}"></div>
      <div class="settings-row"><label>Watchdog label</label><input id="setDashboardWatchdogLabel"${restartAttr(dashboard.watchdogLabel || 'com.actanara.dashboard.watchdog')} value="${escapeHtml(dashboard.watchdogLabel || 'com.actanara.dashboard.watchdog')}"></div>
      <div class="settings-note">${escapeHtml(labels.dashboardRestartNote)}<br>${escapeHtml(labels.restartCommand)}<code>${escapeHtml(DASHBOARD_RESTART_COMMAND)}</code> <button type="button" class="fo-copy-btn" onclick="copyDashboardRestartCommand()">${escapeHtml(labels.copyCommand)}</button><span class="fo-copy-status" id="dashboardRestartCopyStatus" aria-live="polite"></span></div>
    </div>`;
}

function renderScheduleSettings(schedule, agentPrompt) {
  const labels = operatorText();
  const targets = schedule.refreshTargets || {};
  const systemEnabled = schedule.enabled && (schedule.mode || 'system') === 'system';
  const agentEnabled = schedule.enabled && schedule.mode === 'agent';
  return `
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.systemSchedulerMode)}</div>
      <label class="settings-check"><input type="checkbox" id="setScheduleSystemEnabled" ${systemEnabled ? 'checked' : ''} onchange="toggleScheduleMode('system')"> ${escapeHtml(labels.enableSystemScheduler)}</label>
      <div id="systemScheduleSettings" style="${systemEnabled ? '' : 'display:none'}">
      <div class="settings-row"><label>${escapeHtml(labels.timezone)}</label><input id="setTimezone" value="${escapeHtml(schedule.timezone || 'Asia/Hong_Kong')}"></div>
      <div class="settings-row"><label>${escapeHtml(labels.dailyPipelineTime)}</label><input id="setDailyPipelineTime" type="time" value="${escapeHtml(schedule.dailyPipelineTime || '04:00')}"></div>
      <div class="settings-row"><label>${escapeHtml(labels.dashboardAggregationTime)}</label><input id="setDashboardAggregationTime" type="time" value="${escapeHtml(schedule.dashboardAggregationTime || '04:30')}"></div>
      <div class="settings-row"><label>${escapeHtml(labels.systemTimerProvider)}</label><input id="setSystemTimerProvider" value="${escapeHtml((schedule.systemTimer || {}).provider || 'launchd')}"></div>
      <div class="settings-row"><label>${escapeHtml(labels.systemTimerLabel)}</label><input id="setSystemTimerLabel" value="${escapeHtml((schedule.systemTimer || {}).label || 'actanara.daily')}"></div>
      <div class="settings-note">${escapeHtml(labels.systemTimerNote)}</div>
      <div class="settings-timer-actions">
        <button type="button" class="wr-export-btn" onclick="loadSystemTimerPreview()">${escapeHtml(labels.previewSystemTimer)}</button>
        <button type="button" class="wr-export-btn" onclick="installSystemTimer()">${escapeHtml(labels.installUpdate)}</button>
        <button type="button" class="wr-export-btn" onclick="uninstallSystemTimer()">${escapeHtml(labels.uninstall)}</button>
      </div>
      <div id="systemTimerPreview" class="settings-timer-preview" style="display:none"></div>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.autoRefreshTargets)}</div>
      <div class="settings-checks">
        <label class="settings-check"><input type="checkbox" id="setTargetDay" ${targets.currentDay !== false ? 'checked' : ''}> ${escapeHtml(labels.currentDaySnapshot)}</label>
        <label class="settings-check"><input type="checkbox" id="setTargetWeek" ${targets.currentWeek !== false ? 'checked' : ''}> ${escapeHtml(labels.currentWeekSnapshot)}</label>
        <label class="settings-check"><input type="checkbox" id="setTargetMonth" ${targets.currentMonth !== false ? 'checked' : ''}> ${escapeHtml(labels.currentMonthSnapshot)}</label>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.externalAgentMode)}</div>
      <label class="settings-check"><input type="checkbox" id="setScheduleAgentEnabled" ${agentEnabled ? 'checked' : ''} onchange="toggleScheduleMode('agent')"> ${escapeHtml(labels.enableExternalAgentMode)}</label>
      <div id="agentScheduleSettings" style="${agentEnabled ? '' : 'display:none'}">
      <div class="settings-note">${escapeHtml(labels.externalAgentNote)}</div>
      <div class="settings-row"><label>${escapeHtml(labels.prompt)}</label><textarea readonly>${escapeHtml(agentPrompt)}</textarea></div>
      </div>
    </div>`;
}

function toggleScheduleMode(mode) {
  const systemInput = document.getElementById('setScheduleSystemEnabled');
  const agentInput = document.getElementById('setScheduleAgentEnabled');
  if (mode === 'system' && systemInput?.checked && agentInput) agentInput.checked = false;
  if (mode === 'agent' && agentInput?.checked && systemInput) systemInput.checked = false;
  const systemPanel = document.getElementById('systemScheduleSettings');
  const agentPanel = document.getElementById('agentScheduleSettings');
  if (systemPanel) systemPanel.style.display = systemInput?.checked ? '' : 'none';
  if (agentPanel) agentPanel.style.display = agentInput?.checked ? '' : 'none';
}

function renderPathSettings(paths, runtimePath) {
  const labels = operatorText();
  const runtime = paths.runtime || {};
  const diary = paths.diary || {};
  const intermediate = paths.intermediate || {};
  const tasks = paths.tasks || {};
  const rag = paths.rag || {};
  const editable = [
    ['runtime', 'database', 'Runtime SQLite', runtime.database || ''],
    ['runtime', 'snapshots', labels.snapshotsPath, runtime.snapshots || ''],
    ['diary', 'generatedDiary', labels.diaryPath, diary.generatedDiary || ''],
    ['diary', 'reports', labels.reportsPath, diary.reports || ''],
    ['intermediate', 'archives', labels.archivesPath, intermediate.archives || ''],
    ['intermediate', 'taskIntelligence', labels.taskIntelligencePath, intermediate.taskIntelligence || ''],
    ['tasks', 'taskBoard', labels.taskBoardPath, tasks.taskBoard || ''],
    ['rag', 'legacyRagIndex', labels.ragIndexPath, rag.legacyRagIndex || ''],
  ];
  const rows = editable.map(([group, key, label, value]) => `
      <div class="settings-row">
        <label>${escapeHtml(label)}</label>
        <div class="settings-path-control">
          <input data-settings-path-group="${escapeHtml(group)}" data-settings-path-key="${escapeHtml(key)}" data-original-value="${escapeHtml(value)}" data-requires-restart="dashboard" value="${escapeHtml(value)}" ${group === 'runtime' && key === 'actanaraHome' ? 'oninput="updateRuntimePathEditedPreview()"' : ''}>
          <button type="button" class="settings-browse-btn" onclick="openPathBrowser('${escapeHtml(group)}', '${escapeHtml(key)}')">${escapeHtml(labels.browse)}</button>
        </div>
      </div>`).join('');
  return `
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.userPaths)}</div>
      <div class="settings-note">${escapeHtml(labels.userPathsNote)}</div>
      ${rows}
      <div class="settings-section-title">Runtime Home</div>
      <div class="settings-note">${escapeHtml(labels.runtimeHomeNote)}</div>
      <div class="settings-row">
        <label>ACTANARA_HOME</label>
        <div class="settings-path-control">
          <input id="runtimePathCandidate" value="${escapeHtml(runtime.actanaraHome || (runtimePath.selected || {}).actanaraHome || '')}" oninput="updateRuntimePathEditedPreview()">
          <button type="button" class="settings-browse-btn" onclick="openPathBrowser('runtime', 'actanaraHome')">${escapeHtml(labels.browse)}</button>
        </div>
      </div>
      <div id="runtimePathCurrent" class="settings-runtime-status">${renderRuntimePathCurrent(runtimePath || {}, runtimePathEditedValue())}</div>
      <div class="settings-timer-actions">
        <button type="button" class="settings-browse-btn" onclick="loadRuntimePathCurrent()">${escapeHtml(labels.refreshRuntimePath)}</button>
        <button type="button" class="settings-browse-btn" onclick="runtimePathAction('validate')">${escapeHtml(labels.validateRuntimePath)}</button>
        <button type="button" class="settings-browse-btn" onclick="runtimePathAction('use')">${escapeHtml(labels.useRuntimePath)}</button>
        <button type="button" class="settings-browse-btn" onclick="runtimePathAction('initialize')">${escapeHtml(labels.initializeRuntimePath)}</button>
      </div>
      <div id="runtimePathStatus" class="settings-runtime-status" style="display:none"></div>
      <div class="settings-timer-actions">
        <button type="button" class="settings-browse-btn" onclick="checkDiaryPathConsistency()">${escapeHtml(labels.checkDiarySqlite)}</button>
      </div>
      <div id="diaryPathConsistencyPanel" class="settings-runtime-status" style="display:none"></div>
      <div class="settings-section-title">${escapeHtml(labels.diaryProjectionRepair)}</div>
      <div class="settings-note">${escapeHtml(labels.diaryProjectionRepairNote)}</div>
      <div class="settings-row"><label>${escapeHtml(labels.startDate)}</label><input id="diaryRebuildStartDate" type="date"></div>
      <div class="settings-row"><label>${escapeHtml(labels.endDate)}</label><input id="diaryRebuildEndDate" type="date"></div>
      <div class="settings-timer-actions">
        <button type="button" class="settings-browse-btn" onclick="diaryProjectionRebuild(true)">${escapeHtml(labels.dryRunPreview)}</button>
        <button type="button" class="settings-browse-btn" onclick="diaryProjectionRebuild(false)">${escapeHtml(labels.confirmRebuild)}</button>
        <button type="button" class="settings-browse-btn" onclick="loadDiaryProjectionRebuildJobs()">${escapeHtml(labels.refreshRebuildJobs)}</button>
      </div>
      <div id="diaryProjectionRebuildPanel" class="settings-runtime-status" style="display:none"></div>
      <div id="diaryProjectionRebuildJobs" class="settings-runtime-status" style="display:none"></div>
      <div class="settings-section-title">${escapeHtml(labels.dangerousSqliteRebuild)}</div>
      <div class="settings-note">${escapeHtml(labels.sqliteRebuildNote)}</div>
      <div class="settings-timer-actions">
        <button type="button" class="settings-browse-btn" onclick="sqliteCacheRebuild(true)">${escapeHtml(labels.previewRebuildPlan)}</button>
        <button type="button" class="settings-browse-btn" onclick="sqliteCacheRebuild(false)">${escapeHtml(labels.rebuildSqliteCache)}</button>
      </div>
      <div id="sqliteCacheRebuildPanel" class="settings-runtime-status" style="display:none"></div>
    </div>
    <div id="settingsPathBrowser"></div>`;
}

function localIsoDate(date) {
  const selected = date || new Date();
  return selected.getFullYear() + '-' + String(selected.getMonth() + 1).padStart(2, '0') + '-' + String(selected.getDate()).padStart(2, '0');
}

function setDiaryRebuildRangeFromConsistency(data) {
  const startInput = document.getElementById('diaryRebuildStartDate');
  const endInput = document.getElementById('diaryRebuildEndDate');
  if (!startInput || !endInput) return;
  const range = data.mismatchDateRange || data.diskDateRange || data.databaseDateRange || {};
  if (range.startDate) startInput.value = range.startDate;
  if (range.startDate) endInput.value = localIsoDate();
}

async function checkDiaryPathConsistency() {
  const labels = operatorText();
  const panel = document.getElementById('diaryPathConsistencyPanel');
  if (!panel) return;
  panel.style.display = 'block';
  panel.innerHTML = '<div class="wr-loading" style="padding:10px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.checkingDiarySqlite) + '</span></div>';
  try {
    const res = await fetch('/api/settings/diary-path/consistency');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    setDiaryRebuildRangeFromConsistency(data);
    panel.innerHTML = renderDiaryPathConsistency(data);
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(labels.consistencyFailed + e.message) + '</div>';
  }
}

function renderDiaryPathConsistency(data) {
  const labels = operatorText();
  const missing = (data.missingDiskFiles || []).slice(0, 8).map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  const extra = (data.extraDiskFiles || []).slice(0, 8).map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  return [
    '<div class="settings-runtime-line"><b>' + escapeHtml(labels.status) + '</b> ' + escapeHtml(data.status || 'unknown') + ' · readyRows=' + escapeHtml(data.readyRows || 0) + ' · files=' + escapeHtml(data.diskMarkdownFiles || 0) + ' · matched=' + escapeHtml(data.matchedRows || 0) + '</div>',
    '<div class="settings-runtime-line"><b>Diary root</b><code>' + escapeHtml(data.diaryRoot || '') + '</code></div>',
    data.requiresProjectionRefresh ? '<div class="settings-note">' + escapeHtml(labels.projectionRefreshSuggested) + '</div>' : '<div class="settings-note">' + escapeHtml(labels.sqliteMatchesDisk) + '</div>',
    missing ? '<div class="settings-note">' + escapeHtml(labels.sqliteMissingDisk) + '<ul>' + missing + '</ul></div>' : '',
    extra ? '<div class="settings-note">' + escapeHtml(labels.diskMissingSqlite) + '<ul>' + extra + '</ul></div>' : '',
    data.truncated ? '<div class="settings-note">' + escapeHtml(labels.truncatedResults) + '</div>' : ''
  ].join('');
}

function diaryRebuildPayload(dryRun) {
  const labels = operatorText();
  const startDate = document.getElementById('diaryRebuildStartDate')?.value || '';
  const endDate = document.getElementById('diaryRebuildEndDate')?.value || '';
  if (!startDate || !endDate) throw new Error(labels.dateRangeRequired);
  if (endDate < startDate) throw new Error(labels.endDateAfterStart);
  return {startDate, endDate, dryRun, includeUsage: true};
}

async function diaryProjectionRebuild(dryRun) {
  const labels = operatorText();
  const panel = document.getElementById('diaryProjectionRebuildPanel');
  if (!panel) return;
  let payload;
  try {
    payload = diaryRebuildPayload(dryRun);
  } catch (e) {
    panel.style.display = 'block';
    panel.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(e.message) + '</div>';
    return;
  }
  if (!dryRun) {
    const ok = window.confirm(operatorText().confirmDiaryRebuild(payload.startDate, payload.endDate));
    if (!ok) return;
    const confirmationText = 'REBUILD ACTANARA DIARY PROJECTIONS';
    const typed = prompt(labels.confirmationTextRequired + confirmationText);
    if (typed !== confirmationText) {
      panel.style.display = 'block';
      panel.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(labels.confirmationMismatchCancelled) + '</div>';
      return;
    }
    payload.confirmationText = confirmationText;
  }
  panel.style.display = 'block';
  panel.innerHTML = '<div class="wr-loading" style="padding:10px"><div class="wr-spinner"></div><span>' + escapeHtml(dryRun ? labels.generatingDryRun : labels.rebuildingDiaryProjection) + '</span></div>';
  try {
    const res = await fetch('/api/settings/diary-path/rebuild', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    panel.innerHTML = renderDiaryProjectionRebuildResult(data);
    if (!dryRun) {
      await checkDiaryPathConsistency();
      await loadDiaryProjectionRebuildJobs();
    }
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(labels.rebuildFailed + e.message) + '</div>';
  }
}

function renderDiaryProjectionRebuildResult(data) {
  const labels = operatorText();
  const mode = data.dryRun ? 'Dry-run' : labels.executed;
  const summary = data.dryRun
    ? 'wouldDeleteRows=' + escapeHtml(data.wouldDeleteRows || 0) + ' · wouldUpsertDocuments=' + escapeHtml(data.wouldUpsertDocuments || 0)
    : 'runId=' + escapeHtml(data.runId || '') + ' · deletedRows=' + escapeHtml(data.deletedRows || 0) + ' · documents=' + escapeHtml(data.documents || 0);
  const reports = (data.wouldRebuildPeriodReports || [data.pageProjection, data.summaryProjection].filter(Boolean))
    .map(item => '<li><code>' + escapeHtml(item) + '</code></li>').join('');
  const missingDisk = (data.missingDiskFiles || []).slice(0, 8).map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  const missingDb = (data.missingDatabaseRows || []).slice(0, 8).map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  const usage = data.usageCoverage || {};
  const usageRepair = data.usageRepair || null;
  const usageLine = data.usageCoverage
    ? '<div class="settings-runtime-line"><b>' + escapeHtml(labels.tokenHourlyData) + '</b><span>' + Number(usage.events || 0).toLocaleString() + ' events / ' + Number(usage.coveredDays || 0).toLocaleString() + ' days' + (data.wouldRepairUsageEvents ? ' · ' + escapeHtml(labels.willRepairMissingDates) : '') + '</span></div>'
    : '';
  const usageRepairLine = usageRepair
    ? '<div class="settings-runtime-line"><b>' + escapeHtml(labels.tokenRepair) + '</b><span>run #' + Number(usageRepair.runId || 0).toLocaleString() + ' · ' + Number(usageRepair.eventsInWindow || 0).toLocaleString() + ' events · errors ' + Number(usageRepair.errors || 0).toLocaleString() + '</span></div>'
    : '';
  return [
    '<div class="settings-runtime-line"><b>' + mode + '</b> · ' + escapeHtml(data.startDate || '') + ' ~ ' + escapeHtml(data.endDate || '') + ' · ' + summary + '</div>',
    '<div class="settings-runtime-line"><b>Diary root</b><code>' + escapeHtml(data.diaryRoot || '') + '</code></div>',
    usageLine,
    usageRepairLine,
    reports ? '<div class="settings-note">Period reports:<ul>' + reports + '</ul></div>' : '',
    missingDisk ? '<div class="settings-note">' + escapeHtml(labels.missingDiskInRange) + '<ul>' + missingDisk + '</ul></div>' : '',
    missingDb ? '<div class="settings-note">' + escapeHtml(labels.missingDbInRange) + '<ul>' + missingDb + '</ul></div>' : '',
    data.truncated ? '<div class="settings-note">' + escapeHtml(labels.truncatedResults) + '</div>' : '',
    !data.dryRun ? '<div class="settings-note">' + escapeHtml(labels.rebuildCompleteNote) + '</div>' : ''
  ].join('');
}

async function loadDiaryProjectionRebuildJobs() {
  const panel = document.getElementById('diaryProjectionRebuildJobs');
  if (!panel) return;
  panel.style.display = 'block';
  panel.innerHTML = '<div class="settings-note">' + escapeHtml(operatorText().readingRebuildJobs) + '</div>';
  try {
    const res = await fetch('/api/settings/diary-path/rebuild/jobs?limit=10');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    panel.innerHTML = renderDiaryProjectionRebuildJobs(data.jobs || []);
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(operatorText().readRebuildJobsFailed + e.message) + '</div>';
  }
}

async function sqliteCacheRebuild(dryRun) {
  const labels = operatorText();
  const panel = document.getElementById('sqliteCacheRebuildPanel');
  if (!panel) return;
  const payload = {dryRun};
  if (!dryRun) {
    const required = 'REBUILD ACTANARA SQLITE CACHE';
    const typed = window.prompt(labels.sqliteRebuildPrompt + required);
    if (typed !== required) {
      panel.style.display = 'block';
      panel.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(labels.confirmationMismatchCancelled) + '</div>';
      return;
    }
    payload.confirmationText = typed;
  }
  panel.style.display = 'block';
  panel.innerHTML = '<div class="wr-loading" style="padding:10px"><div class="wr-spinner"></div><span>' + escapeHtml(dryRun ? labels.generatingSqlitePlan : labels.rebuildingSqlite) + '</span></div>';
  try {
    const res = await fetch('/api/settings/sqlite-cache/rebuild', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    panel.innerHTML = renderSqliteCacheRebuildResult(data);
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(labels.sqliteRebuildFailed + e.message) + '</div>';
  }
}

function renderSqliteCacheRebuildResult(data) {
  const labels = operatorText();
  const range = data.rebuildRange || {};
  const usage = data.usageIngestion || {};
  const backup = data.backup || {};
  return [
    '<div class="settings-runtime-line"><b>' + (data.dryRun ? 'Dry-run' : escapeHtml(data.status || 'completed')) + '</b> · ' + escapeHtml(range.startDate || '') + ' ~ ' + escapeHtml(range.endDate || '') + '</div>',
    '<div class="settings-runtime-line"><b>Database</b><code>' + escapeHtml(data.database || '') + '</code></div>',
    '<div class="settings-runtime-line"><b>Diary root</b><code>' + escapeHtml(data.diaryRoot || '') + '</code></div>',
    '<div class="settings-runtime-line"><b>' + escapeHtml(labels.diaryDates) + '</b><span>' + Number(data.diaryDates || 0).toLocaleString() + '</span></div>',
    data.dryRun ? '<div class="settings-note">' + escapeHtml(labels.confirmationTextRequired) + '<code>' + escapeHtml(data.confirmationTextRequired || '') + '</code></div>' : '',
    backup.backupDir ? '<div class="settings-runtime-line"><b>Backup</b><code>' + escapeHtml(backup.backupDir) + '</code></div>' : '',
    usage.runId ? '<div class="settings-runtime-line"><b>' + escapeHtml(labels.usageIngestion) + '</b><span>run #' + Number(usage.runId).toLocaleString() + ' · ' + Number(usage.eventsInWindow || 0).toLocaleString() + ' events · errors ' + Number(usage.errors || 0).toLocaleString() + '</span></div>' : '',
    data.runId ? '<div class="settings-runtime-line"><b>' + escapeHtml(labels.projectionRun) + '</b><span>#' + Number(data.runId).toLocaleString() + '</span></div>' : '',
    data.warning ? '<div class="settings-note">' + escapeHtml(data.warning) + '</div>' : ''
  ].join('');
}

function diaryRebuildJobProgress(status) {
  if (status === 'queued') return 20;
  if (status === 'running') return 60;
  return 100;
}

function renderDiaryProjectionRebuildJobs(jobs) {
  if (!jobs.length) return '<div class="settings-note">' + escapeHtml(operatorText().noRebuildJobs) + '</div>';
  return jobs.map(job => {
    const meta = job.metadata || {};
    const progress = diaryRebuildJobProgress(job.status);
    const failed = job.status === 'failed';
    return [
      '<div class="settings-job-row">',
      '<div class="settings-runtime-line"><b>runId=' + escapeHtml(job.id) + '</b> · ' + escapeHtml(job.status || 'unknown') + ' · ' + escapeHtml(meta.startDate || '') + ' ~ ' + escapeHtml(meta.endDate || '') + '</div>',
      '<div class="settings-progress"><div class="settings-progress-bar ' + (failed ? 'failed' : '') + '" style="width:' + progress + '%"></div></div>',
      '<div class="settings-note">businessDate=' + escapeHtml(job.business_date || '') + ' · started=' + escapeHtml(job.started_at || '') + (job.completed_at ? ' · completed=' + escapeHtml(job.completed_at) : '') + '</div>',
      job.error_summary ? '<div class="fo-job-error">error: ' + escapeHtml(job.error_summary) + '</div>' : '',
      '</div>'
    ].join('');
  }).join('');
}

function runtimePathEditedValue() {
  return document.getElementById('runtimePathCandidate')?.value || '';
}

function renderRuntimePathCurrent(data, editedPath) {
  const labels = operatorText();
  const selected = data.selected || {};
  const validation = data.validation || {};
  const selectedHome = selected.actanaraHome || '—';
  const edited = editedPath || selectedHome;
  const changed = Boolean(edited && selected.actanaraHome && edited !== selected.actanaraHome);
  const issues = (validation.issues || []).map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  return [
    '<div class="settings-runtime-line"><b>' + escapeHtml(labels.currentSelection) + '</b><code>' + escapeHtml(selectedHome) + '</code></div>',
    '<div class="settings-runtime-line"><b>' + escapeHtml(labels.editedValue) + '</b><code id="runtimePathEditedPreview">' + escapeHtml(edited || '—') + '</code></div>',
    '<div class="settings-runtime-flags">',
    '<span class="settings-runtime-chip ' + (validation.valid ? 'ok' : 'warn') + '">valid=' + Boolean(validation.valid) + '</span>',
    '<span class="settings-runtime-chip ' + (validation.initialized ? 'ok' : 'warn') + '">initialized=' + Boolean(validation.initialized) + '</span>',
    '<span class="settings-runtime-chip ' + (validation.writable ? 'ok' : 'warn') + '">writable=' + Boolean(validation.writable) + '</span>',
    '<span class="settings-runtime-chip ' + (changed ? 'warn' : 'ok') + '">' + (changed ? 'edited differs from selected' : 'edited matches selected') + '</span>',
    data.envOverride ? '<span class="settings-runtime-chip warn">ACTANARA_HOME env override</span>' : '',
    '</div>',
    data.locationFile ? '<div class="settings-note">Location file: <code>' + escapeHtml(data.locationFile) + '</code></div>' : '',
    issues ? '<ul>' + issues + '</ul>' : ''
  ].join('');
}

function updateRuntimePathEditedPreview() {
  const preview = document.getElementById('runtimePathEditedPreview');
  if (preview) preview.textContent = runtimePathEditedValue() || '—';
}

async function loadRuntimePathCurrent() {
  const labels = operatorText();
  const panel = document.getElementById('runtimePathCurrent');
  if (!panel) return;
  panel.innerHTML = '<div class="wr-loading" style="padding:10px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingRuntimePath) + '</span></div>';
  try {
    const res = await fetch('/api/settings/runtime-path');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    panel.innerHTML = renderRuntimePathCurrent(await res.json(), runtimePathEditedValue());
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(labels.runtimePathReadFailed + e.message) + '</div>';
  }
}

async function runtimePathAction(mode) {
  const labels = operatorText();
  const status = document.getElementById('runtimePathStatus');
  const homeInput = document.getElementById('runtimePathCandidate');
  if (!status || !homeInput) return;
  const path = homeInput.value || '';
  status.style.display = 'block';
  status.innerHTML = '<div class="wr-loading" style="padding:10px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.processing) + '</span></div>';
  try {
    let data;
    if (mode === 'validate') {
      const res = await fetch('/api/settings/runtime-path/validate?path=' + encodeURIComponent(path));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      data = await res.json();
    } else {
      const confirmationText = 'SELECT ACTANARA RUNTIME PATH';
      const typed = prompt(labels.runtimePathConfirmationPrompt + confirmationText);
      if (typed !== confirmationText) {
        status.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(labels.confirmationMismatchCancelled) + '</div>';
        return;
      }
      const res = await fetch('/api/settings/runtime-path/select', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path, mode, confirmationText})
      });
      data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      if (data.selected && data.selected.actanaraHome) homeInput.value = data.selected.actanaraHome;
      await loadRuntimePathCurrent();
    }
    status.innerHTML = renderRuntimePathStatus(data, mode);
  } catch (e) {
    status.innerHTML = '<div class="fo-job-error" style="padding:10px">' + escapeHtml(labels.pathOperationFailed + e.message) + '</div>';
  }
}

function renderRuntimePathStatus(data, mode) {
  const validation = data.validation || {};
  const selected = data.selected || {};
  const issues = (validation.issues || []).map(item => '<li>' + escapeHtml(item) + '</li>').join('');
  const importResult = data.importResult ? '<div>import copied=' + escapeHtml(data.importResult.copied) + ' matched=' + escapeHtml(data.importResult.matched) + ' conflicts=' + escapeHtml((data.importResult.conflicts || []).length) + '</div>' : '';
  return '<div class="settings-runtime-line"><b>' + escapeHtml(mode) + '</b> · valid=' + Boolean(validation.valid) + ' · initialized=' + Boolean(validation.initialized) + ' · writable=' + Boolean(validation.writable) + '</div>' +
    '<code>' + escapeHtml(selected.actanaraHome || validation.candidate || '') + '</code>' +
    (issues ? '<ul>' + issues + '</ul>' : '') +
    importResult +
    (data.audit ? '<div>audit: <code>' + escapeHtml(data.audit.path || '') + '</code></div>' : '');
}

async function openPathBrowser(group, key, pathOverride) {
  const labels = operatorText();
  const input = document.querySelector('[data-settings-path-group="' + CSS.escape(group) + '"][data-settings-path-key="' + CSS.escape(key) + '"]');
  if (!input) return;
  const selectedPath = pathOverride || input.value || '/';
  const host = document.querySelector('[data-pane="paths"]');
  if (!host) return;
  let panel = document.getElementById('settingsPathBrowser');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'settingsPathBrowser';
    panel.className = 'path-browser';
    host.appendChild(panel);
  }
  panel.innerHTML = '<div class="wr-loading" style="padding:20px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingPath) + '</span></div>';
  try {
    const res = await fetch('/api/settings/path-browser?path=' + encodeURIComponent(selectedPath));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    panel.innerHTML = renderPathBrowser(group, key, data);
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error" style="padding:12px">' + escapeHtml(labels.pathReadFailed + e.message) + '</div>';
  }
}

function renderPathBrowser(group, key, data) {
  const labels = operatorText();
  const rows = [];
  if (data.parent) {
    rows.push(`<button type="button" class="path-browser-row" onclick="openPathBrowser('${escapeJs(group)}','${escapeJs(key)}','${escapeJs(data.parent)}')"><span class="path-browser-type">↖</span><span>${escapeHtml(labels.parentDirectory)}</span></button>`);
  }
  rows.push(...(data.entries || []).map(item => {
    const icon = item.type === 'directory' ? '📁' : '📄';
    return `<button type="button" class="path-browser-row" onclick="pathBrowserChoose('${escapeJs(group)}','${escapeJs(key)}','${escapeJs(item.path)}','${escapeJs(item.type)}')"><span class="path-browser-type">${icon}</span><span>${escapeHtml(item.name)}</span></button>`;
  }));
  return `<div class="path-browser-header"><div class="path-browser-current">${escapeHtml(data.current || '')}</div><button class="settings-browse-btn" onclick="pathBrowserUseCurrent('${escapeJs(group)}','${escapeJs(key)}','${escapeJs(data.current || '')}')">${escapeHtml(labels.chooseCurrentDirectory)}</button><button class="settings-browse-btn" onclick="closePathBrowser()">${escapeHtml(labels.cancel)}</button></div><div class="path-browser-list">${rows.join('')}</div>`;
}

function pathBrowserChoose(group, key, selectedPath, type) {
  if (type === 'directory') {
    openPathBrowser(group, key, selectedPath);
    return;
  }
  pathBrowserUseCurrent(group, key, selectedPath);
}

function pathBrowserUseCurrent(group, key, selectedPath) {
  const input = group === 'runtime' && key === 'actanaraHome'
    ? document.getElementById('runtimePathCandidate')
    : document.querySelector('[data-settings-path-group="' + CSS.escape(group) + '"][data-settings-path-key="' + CSS.escape(key) + '"]');
  if (input) input.value = selectedPath;
  closePathBrowser();
}

function closePathBrowser() {
  const panel = document.getElementById('settingsPathBrowser');
  if (panel) panel.remove();
}

function renderRuntimeSourceSettings(runtimeSources) {
  const text = operatorText();
  const labels = {
    dashboardReadSource: 'Dashboard read source',
    reportReadSource: 'Report read source',
    diaryMetricsSource: 'Diary metrics source',
    diaryMemorySource: 'Diary memory source',
    diaryTasksSource: 'Diary tasks source',
    taskAuditSink: 'Task audit sink',
    llmGeneration: 'LLM generation',
  };
  const rows = Object.keys(labels).map(key => {
    const value = runtimeSources[key] || 'foundation';
    const legacyNote = value === 'legacy' ? '<div class="settings-note">' + escapeHtml(text.runtimeSourceRetiredNote) + '</div>' : '';
    return `<div class="settings-row"><label>${escapeHtml(labels[key])}</label><select data-runtime-source-key="${escapeHtml(key)}">
      <option value="foundation" selected>foundation</option>
    </select></div>${legacyNote}`;
  }).join('');
  return '<div class="settings-section">' +
    '<div class="settings-section-title">' + escapeHtml(text.runtimeSourcesTitle) + '</div>' +
    '<div class="settings-note">' + escapeHtml(text.runtimeSourcesNote) + '</div>' +
    rows +
    '</div>';
}

function renderWorkspaceAttributionSettings() {
  return '<div class="settings-section">' +
    '<div class="settings-section-title">Workspace Attribution</div>' +
    '<div class="settings-note">管理 AI Assets、周报、月报使用的 workspace 归属规则。保存规则后需要刷新 AI Assets cache 与 period projection 才会反映到历史统计。</div>' +
    '<div class="settings-runtime-actions"><button type="button" class="wr-export-btn" onclick="loadWorkspaceAttributionSettings()">刷新状态</button></div>' +
    '<div id="workspaceAttributionStatus" class="settings-runtime-status">尚未读取</div>' +
    '</div>' +
    '<div class="settings-section">' +
      '<div class="settings-section-title">添加归属规则</div>' +
      '<div class="settings-row"><label>Rule Type</label><select id="workspaceAttributionRuleType"><option value="path">path</option><option value="alias">alias</option><option value="container">container</option></select></div>' +
      '<div class="settings-row"><label>Tool</label><select id="workspaceAttributionTool"><option value="">all</option><option value="codex">Codex</option><option value="claude-code">Claude Code</option><option value="gemini-cli">Gemini CLI</option><option value="openclaw">OpenClaw</option><option value="opencode">OpenCode</option><option value="antigravity">Antigravity</option><option value="cursor">Cursor</option></select></div>' +
      '<div class="settings-row"><label>Workspace Path</label><input id="workspaceAttributionPath" placeholder="~/Projects/TokenClock"></div>' +
      '<div class="settings-row"><label>Workspace Name</label><input id="workspaceAttributionName" placeholder="留空则使用项目 metadata"></div>' +
      '<div class="settings-row"><label>Alias From</label><input id="workspaceAttributionAliasSource" placeholder="TokenClock-normal"></div>' +
      '<div class="settings-row"><label>Alias To</label><input id="workspaceAttributionAliasTarget" placeholder="TokenClock"></div>' +
      '<div class="settings-row"><label>Container Name</label><input id="workspaceAttributionContainerName" placeholder="tmp_workspace"></div>' +
      '<div class="settings-actions"><span class="settings-status" id="workspaceAttributionRuleStatus"></span><button type="button" class="wr-export-btn" onclick="previewWorkspaceAttributionRule()">计划预览</button><button type="button" class="wr-export-btn" onclick="applyWorkspaceAttributionRule()">保存规则</button></div>' +
      '<div id="workspaceAttributionRulePreview" class="settings-runtime-status" style="display:none"></div>' +
    '</div>';
}

async function loadWorkspaceAttributionSettings() {
  const box = document.getElementById('workspaceAttributionStatus');
  if (!box) return;
  box.innerHTML = '读取中…';
  try {
    const res = await fetch('/api/settings/workspace-attribution');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    box.innerHTML = renderWorkspaceAttributionStatus(data);
  } catch (e) {
    box.innerHTML = '<div class="fo-job-error">' + escapeHtml(e.message) + '</div>';
  }
}

function renderWorkspaceAttributionStatus(data) {
  const qa = data.qa || {};
  const catalog = data.catalog || {};
  const rules = (data.rules || {}).rules || [];
  const projects = catalog.projects || [];
  const usage = data.workspaceUsage || [];
  const findings = qa.findings || [];
  const sourceStats = qa.transcriptInferredTokensBySource || {};
  const sourceRows = Object.keys(sourceStats).sort().map(key =>
    '<span class="settings-runtime-chip ok">' + escapeHtml(key) + '=' + escapeHtml(wrFormatTokens(sourceStats[key])) + '</span>'
  ).join('');
  const usageRows = usage.slice(0, 8).map(row =>
    '<div class="settings-runtime-line"><b>' + escapeHtml(row.tool || '') + ' / ' + escapeHtml(row.name || '') + '</b> ' + escapeHtml(wrFormatTokens(row.tokens || 0)) + '</div>'
  ).join('');
  const projectRows = projects.slice(0, 8).map(project =>
    '<div class="settings-runtime-line"><b>' + escapeHtml(project.display_name || '') + '</b><code>' + escapeHtml(project.root_path || '') + '</code></div>'
  ).join('');
  const ruleRows = rules.slice(0, 12).map(rule =>
    '<div class="settings-runtime-line"><span class="settings-runtime-chip ok">' + escapeHtml(rule.type || '') + '</span> ' +
    escapeHtml(rule.workspace || rule.target || rule.name || rule.source || '') +
    (rule.workspacePath ? '<code>' + escapeHtml(rule.workspacePath) + '</code>' : '') + '</div>'
  ).join('');
  const findingRows = findings.slice(0, 8).map(item =>
    '<div class="settings-runtime-line"><span class="settings-runtime-chip warn">' + escapeHtml(item.id || '') + '</span> ' + escapeHtml(item.tool || '') + ' / ' + escapeHtml(item.workspace || '') + ' · ' + escapeHtml(wrFormatTokens(item.tokens || 0)) + '</div>'
  ).join('');
  return '<div class="settings-runtime-line"><b>QA</b> ' + escapeHtml(qa.status || 'unknown') +
    ' · hidden=' + escapeHtml(wrFormatTokens(qa.hiddenTokens || 0)) +
    ' · low=' + escapeHtml(wrFormatTokens(qa.lowConfidenceTokens || 0)) + '</div>' +
    '<div class="settings-runtime-flags">' + sourceRows + '</div>' +
    (findingRows ? '<div class="settings-section-title">Findings</div>' + findingRows : '') +
    '<div class="settings-section-title">Top Workspace Usage</div>' + (usageRows || '<div class="settings-note">暂无 workspace usage</div>') +
    '<div class="settings-section-title">Project Catalog</div>' + (projectRows || '<div class="settings-note">暂无 catalog 项目</div>') +
    '<div class="settings-section-title">Rules</div>' + (ruleRows || '<div class="settings-note">暂无用户规则</div>') +
    '<div class="settings-runtime-line"><b>Catalog</b><code>' + escapeHtml(((data.paths || {}).catalog) || '') + '</code></div>' +
    '<div class="settings-runtime-line"><b>Rules</b><code>' + escapeHtml(((data.paths || {}).rules) || '') + '</code></div>';
}

function collectWorkspaceAttributionRulePayload() {
  const type = document.getElementById('workspaceAttributionRuleType')?.value || 'path';
  const base = {
    type,
    tool: document.getElementById('workspaceAttributionTool')?.value || '',
    reason: 'manual-settings-workspace-attribution'
  };
  if (type === 'alias') {
    return {
      ...base,
      source: document.getElementById('workspaceAttributionAliasSource')?.value || '',
      target: document.getElementById('workspaceAttributionAliasTarget')?.value || ''
    };
  }
  if (type === 'container') {
    return {
      ...base,
      name: document.getElementById('workspaceAttributionContainerName')?.value || ''
    };
  }
  return {
    ...base,
    workspacePath: document.getElementById('workspaceAttributionPath')?.value || '',
    workspace: document.getElementById('workspaceAttributionName')?.value || ''
  };
}

async function previewWorkspaceAttributionRule() {
  await submitWorkspaceAttributionRule(true);
}

async function applyWorkspaceAttributionRule() {
  await submitWorkspaceAttributionRule(false);
}

async function submitWorkspaceAttributionRule(dryRun) {
  const status = document.getElementById('workspaceAttributionRuleStatus');
  const preview = document.getElementById('workspaceAttributionRulePreview');
  if (status) status.textContent = dryRun ? '预览中…' : '保存中…';
  if (preview) preview.style.display = 'block';
  try {
    const res = await fetch(dryRun ? '/api/settings/workspace-attribution/rules/preview' : '/api/settings/workspace-attribution/rules', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(collectWorkspaceAttributionRulePayload())
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (status) status.textContent = dryRun ? '预览完成' : '规则已保存；请刷新 AI Assets cache 以重算历史统计';
    if (preview) preview.innerHTML = renderWorkspaceAttributionRulePreview(data);
    if (!dryRun) loadWorkspaceAttributionSettings();
  } catch (e) {
    if (status) status.textContent = dryRun ? '预览失败' : '保存失败';
    if (preview) preview.innerHTML = '<div class="fo-job-error">' + escapeHtml(e.message) + '</div>';
  }
}

function renderWorkspaceAttributionRulePreview(data) {
  const rule = data.rule || {};
  return '<div class="settings-runtime-line"><b>' + (data.dryRun ? 'Dry-run' : 'Saved') + '</b> · duplicate=' + escapeHtml(String(Boolean(data.duplicate))) + '</div>' +
    '<div class="settings-runtime-line"><b>Rule</b> ' + escapeHtml(rule.type || '') + ' · ' + escapeHtml(rule.tool || 'all') + ' · ' + escapeHtml(rule.workspace || rule.target || rule.name || '') + '</div>' +
    (rule.source ? '<div class="settings-runtime-line"><b>Alias</b> ' + escapeHtml(rule.source) + ' -> ' + escapeHtml(rule.target || '') + '</div>' : '') +
    (rule.name ? '<div class="settings-runtime-line"><b>Container</b> ' + escapeHtml(rule.name) + '</div>' : '') +
    (rule.workspacePath ? '<div class="settings-runtime-line"><b>Path</b><code>' + escapeHtml(rule.workspacePath) + '</code></div>' : '') +
    '<div class="settings-runtime-line"><b>Rules</b> ' + escapeHtml(data.ruleCountBefore || 0) + ' -> ' + escapeHtml(data.ruleCountAfter || 0) + '</div>' +
    ((data.sideEffects || []).length ? '<div class="settings-runtime-flags">' + data.sideEffects.map(item => '<span class="settings-runtime-chip warn">' + escapeHtml(item) + '</span>').join('') + '</div>' : '');
}

function renderExternalToolSettings(externalTools) {
  const labels = operatorText();
  const groups = Object.keys(externalTools || {});
  if (!groups.length) return '<div class="settings-note">' + escapeHtml(labels.noExternalTools) + '</div>';
  return '<div class="settings-section">' +
    '<div class="settings-section-title">' + escapeHtml(labels.externalToolPaths) + '</div>' +
    '<div class="settings-note">' + escapeHtml(labels.externalToolPathsNote) + '</div>' +
    groups.map(tool => {
      const values = externalTools[tool] || {};
      const rows = Object.keys(values).map(key => {
        const value = values[key];
        if (Array.isArray(value)) {
          return `<div class="settings-row"><label>${escapeHtml(key)}</label><textarea data-external-tool="${escapeHtml(tool)}" data-external-key="${escapeHtml(key)}" data-external-type="list">${escapeHtml(value.join('\n'))}</textarea></div>`;
        }
        return `<div class="settings-row"><label>${escapeHtml(key)}</label><input data-external-tool="${escapeHtml(tool)}" data-external-key="${escapeHtml(key)}" value="${escapeHtml(value || '')}"></div>`;
      }).join('');
      return '<div class="settings-path-group"><div class="settings-path-group-title">' + escapeHtml(tool) + '</div>' + rows + '</div>';
    }).join('') +
    '</div>';
}

function renderPipelineSettings(pipeline) {
  const labels = operatorText();
  const stepTimeouts = pipeline.stepTimeouts || {};
  const stepRows = Object.keys(stepTimeouts).map(key => `
    <div class="settings-row">
      <label>${escapeHtml(key)}</label>
      <input data-pipeline-step-timeout="${escapeHtml(key)}" type="number" min="1" value="${escapeHtml(stepTimeouts[key])}">
    </div>`).join('');
  return `
    <div class="settings-section">
      <div class="settings-section-title">Daily pipeline</div>
      <div class="settings-row"><label>Stable command</label><input id="setPipelineStableCommand" value="${escapeHtml(pipeline.stableCommand || 'python advanced/pipeline/run_daily_pipeline.py [YYYY-MM-DD]')}"></div>
      <div class="settings-row"><label>Python</label><input id="setPipelinePython" value="${escapeHtml(pipeline.pythonExecutable || 'python3')}"></div>
      <div class="settings-row"><label>Working directory</label><input id="setPipelineWorkingDirectory" value="${escapeHtml(pipeline.workingDirectory || '')}"></div>
      <div class="settings-row"><label>Date argument</label><input id="setPipelineDateArgument" value="${escapeHtml(pipeline.dailyDateArgument || 'YYYY-MM-DD')}"></div>
      <div class="settings-row"><label>Skip final nova-RAG env</label><input id="setPipelineSkipFinalRagEnv" value="${escapeHtml(pipeline.skipFinalRagEnv || 'ACTANARA_PIPELINE_SKIP_FINAL_RAG')}"></div>
      <div class="settings-row"><label>Thinking mode</label><select id="setPipelineThinkingMode">
        ${['off','low','medium'].map(mode => '<option value="' + mode + '" ' + ((pipeline.thinkingMode || 'off') === mode ? 'selected' : '') + '>' + mode + '</option>').join('')}
      </select></div>
      <div class="settings-row"><label>Default step timeout</label><input id="setPipelineStepTimeoutSeconds" type="number" min="1" value="${escapeHtml(pipeline.stepTimeoutSeconds || 1800)}"></div>
      <div class="settings-row"><label>Total watchdog</label><input id="setPipelineTotalWatchdogSeconds" type="number" min="1" value="${escapeHtml(pipeline.totalWatchdogSeconds || 7200)}"></div>
      <div class="settings-note">${escapeHtml(labels.pipelineSettingsNote)}</div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">Step timeouts</div>
      ${stepRows || '<div class="settings-note">' + escapeHtml(labels.noStepTimeouts) + '</div>'}
    </div>`;
}

function escapeJs(text) {
  return String(text || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function renderFeatureSettings(features) {
  const text = operatorText();
  const labels = {
    pipeline: text.featureDailyPipeline,
    dashboard: text.featureDashboard,
    foundationSnapshots: 'Foundation snapshots',
    rag: 'nova-RAG',
    novaTask: 'Nova-Task',
    embeddingServer: 'Embedding server',
    taskAuditSink: 'Task audit sink',
    llmGeneration: 'LLM generation'
  };
  return '<div class="settings-note">' + escapeHtml(text.featureSettingsNote) + '</div><div class="settings-checks">' +
    Object.keys(labels).map(key => '<label class="settings-check"><input type="checkbox" data-feature-key="' + key + '" ' + (features[key] ? 'checked' : '') + '> ' + labels[key] + '</label>').join('') +
    '</div>';
}

function ragProfileLabel(profile) {
  profile = profile || {};
  const mode = profile.mode || '—';
  const provider = profile.providerId || mode;
  const model = profile.model || '—';
  const dimension = profile.dimension || '—';
  return mode + ' / ' + provider + ' / ' + model + ' / ' + dimension;
}

const NOVA_RAG_EMBEDDING_MODELS = [
  {language: 'zh', model: 'intfloat/multilingual-e5-small', dimension: 384, label: '中文/多语 384 · multilingual E5 small'},
  {language: 'zh', model: 'BAAI/bge-large-zh-v1.5', dimension: 1024, label: '中文 1024 · BGE large zh'},
  {language: 'en', model: 'all-MiniLM-L6-v2', dimension: 384, label: 'English 384 · MiniLM L6'},
  {language: 'en', model: 'BAAI/bge-large-en-v1.5', dimension: 1024, label: 'English 1024 · BGE large en'},
];

function novaRagModelOptionsForLanguage(language) {
  if (language === 'mixed') return NOVA_RAG_EMBEDDING_MODELS;
  return NOVA_RAG_EMBEDDING_MODELS.filter(item => item.language === language);
}

function novaRagModelOptionHtml(language, selectedModel) {
  const options = novaRagModelOptionsForLanguage(language || 'zh');
  const selected = selectedModel && options.some(item => item.model === selectedModel)
    ? selectedModel
    : (options[0] || NOVA_RAG_EMBEDDING_MODELS[0]).model;
  return options.map(item =>
    '<option value="' + escapeHtml(item.model) + '" data-dimension="' + escapeHtml(item.dimension) + '" ' +
    (item.model === selected ? 'selected' : '') + '>' + escapeHtml(item.label + ' · ' + item.model) + '</option>'
  ).join('');
}

function updateRagMigrationModelOptions() {
  const lang = document.getElementById('ragMigrationLanguage')?.value || 'zh';
  const select = document.getElementById('ragMigrationModel');
  if (!select) return;
  const previous = select.value || '';
  select.innerHTML = novaRagModelOptionHtml(lang, previous);
  updateRagMigrationDimensionFromModel();
}

function updateRagMigrationDimensionFromModel() {
  const select = document.getElementById('ragMigrationModel');
  const dimension = document.getElementById('ragMigrationDimension');
  if (!select || !dimension) return;
  const selected = select.options[select.selectedIndex];
  dimension.value = selected?.dataset?.dimension || '';
}

function updateRagMigrationFieldVisibility() {
  const initMode = document.getElementById('ragMigrationInitMode')?.value === '1';
  const mode = document.getElementById('ragMigrationMode')?.value || 'local';
  const cloudOnly = mode === 'cloud';
  [
    ['ragMigrationProviderIdRow', cloudOnly],
    ['ragMigrationEndpointRow', cloudOnly],
    ['ragMigrationApiKeyEnvRow', cloudOnly],
    ['ragMigrationConfirmationRow', !initMode],
  ].forEach(([id, visible]) => {
    const row = document.getElementById(id);
    if (row) row.style.display = visible ? '' : 'none';
  });
}

function isNovaRagInitialized(status) {
  status = status || window._lastRagStatus || {};
  const activeProfile = ((status.profile || {}).active || {});
  const v2 = status.v2 || {};
  return Boolean(activeProfile.model || v2.ready || v2.activeIndexPath || v2.manifestExists);
}

function setNovaRagPowerBusy(label) {
  const btn = document.getElementById('ragPowerBtn');
  if (!btn) return;
  btn.disabled = true;
  btn.innerHTML = '<span class="wr-spinner" style="width:12px;height:12px;margin-right:6px;vertical-align:-2px"></span>' + escapeHtml(label);
}

function updateNovaRagPowerButton(status) {
  const labels = ragUiText();
  const btn = document.getElementById('ragPowerBtn');
  if (!btn) return;
  status = status || window._lastRagStatus || {};
  const productEnabled = status.productEnabled !== false;
  const initialized = isNovaRagInitialized(status);
  btn.disabled = false;
  btn.classList.toggle('secondary', productEnabled);
  if (productEnabled) {
    btn.textContent = labels.powerDisable;
  } else if (initialized) {
    btn.textContent = labels.powerEnable;
  } else {
    btn.textContent = labels.powerInit;
  }
  btn.title = productEnabled
    ? labels.powerDisableTitle
    : (initialized ? labels.powerEnableTitle : labels.powerInitTitle);
}

function renderRagSettings(rag, status) {
  const labels = ragUiText();
  rag = rag || {};
  status = status || {};
  window._lastRagSettings = rag;
  window._lastRagStatus = status;
  const retrieval = rag.retrieval || {};
  const profile = status.profile || {};
  const configuredProfile = profile.configured || {};
  const activeProfile = profile.active || {};
  const configuredLabel = ragProfileLabel(configuredProfile);
  const activeLabel = activeProfile && activeProfile.model ? ragProfileLabel(activeProfile) : labels.noActiveProfile;
  return `
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.instantParams)}</div>
      <div class="settings-runtime-status">
        <div class="settings-runtime-line"><b>${escapeHtml(labels.configured)}</b> ${escapeHtml(configuredLabel)}</div>
        <div class="settings-runtime-line"><b>${escapeHtml(labels.activeIndex)}</b> ${escapeHtml(activeLabel)}</div>
        <div class="settings-runtime-line"><b>Policy</b> ${escapeHtml(labels.policy)}</div>
      </div>
      <div class="settings-row"><label>Top K</label><input id="setRagTopK" type="number" min="1" max="50" value="${escapeHtml(retrieval.topK || 8)}"></div>
      <div class="settings-row"><label>${escapeHtml(labels.timeHalfLife)}</label><input id="setRagHalfLife" type="number" min="1" value="${escapeHtml(retrieval.recencyHalfLifeDays || 7)}"></div>
      <button type="button" class="settings-browse-btn" onclick="saveRagSettingsPanel()">${escapeHtml(labels.saveInstantParams)}</button>
    </div>`;
}

function rerenderRagSettingsPanel() {
  const pane = document.getElementById('ragSearchSettings');
  if (!pane) return;
  const rag = collectRagSettingsFromModal();
  const status = Object.assign({}, window._lastRagStatus || {});
  pane.innerHTML = renderRagSettings(rag, status);
}

function rerenderRagSettingsFromModal() {
  rerenderRagSettingsPanel();
}

function renderFutureSettings(todos) {
  const labels = operatorText();
  const cli = todos.cliCommands || [];
  return `
    <div class="settings-section">
      <div class="settings-section-title">GitHub</div>
      <div class="settings-note">${escapeHtml(labels.githubSectionNote)}</div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.i18nSwitch)}</div>
      <div class="settings-note">${escapeHtml(todos.i18n || 'todo')}</div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.llmProviderList)}</div>
      <div class="settings-note">${escapeHtml(labels.llmProviderListNote)}</div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">${escapeHtml(labels.cliReserved)}</div>
      <textarea readonly>${escapeHtml(cli.join('\n'))}</textarea>
    </div>`;
}

async function saveSettingsModal() {
  // Static contract anchor: 启动参数变更需重启 Dashboard.
  const status = document.getElementById('settingsSaveStatus');
  if (status) status.textContent = operatorText().saving;
  const restartRequired = Array.from(document.querySelectorAll('[data-requires-restart]')).some(input => String(input.value || '') !== String(input.dataset.originalValue || ''));
  const features = {};
  document.querySelectorAll('[data-feature-key]').forEach(input => {
    features[input.dataset.featureKey] = input.checked;
  });
  const payload = {
    general: collectGeneralSettingsFromModal(),
    dashboard: collectDashboardSettingsFromModal(),
    schedule: {
      enabled: Boolean(document.getElementById('setScheduleSystemEnabled')?.checked || document.getElementById('setScheduleAgentEnabled')?.checked),
      mode: document.getElementById('setScheduleAgentEnabled')?.checked ? 'agent' : 'system',
      timezone: document.getElementById('setTimezone')?.value || 'Asia/Hong_Kong',
      dailyPipelineTime: document.getElementById('setDailyPipelineTime')?.value || '04:00',
      dashboardAggregationTime: document.getElementById('setDashboardAggregationTime')?.value || '04:30',
      refreshTargets: {
        currentDay: document.getElementById('setTargetDay')?.checked || false,
        currentWeek: document.getElementById('setTargetWeek')?.checked || false,
        currentMonth: document.getElementById('setTargetMonth')?.checked || false,
      },
      systemTimer: {
        provider: document.getElementById('setSystemTimerProvider')?.value || 'launchd',
        label: document.getElementById('setSystemTimerLabel')?.value || 'actanara.daily',
      }
    },
    paths: collectPathSettingsFromModal(),
    runtimeSources: collectRuntimeSourceSettingsFromModal(),
    pipeline: collectPipelineSettingsFromModal(),
    externalTools: collectExternalToolSettingsFromModal(),
    features,
  };
  try {
    const bundle = {settings: payload};
    if (document.getElementById('llmProviderName')) {
      bundle.llmProvider = collectLlmProviderSettingsFromModal();
    }
    const res = await fetch('/api/settings/bundle', {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(bundle)});
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + res.status));
    }
    const freshRes = await fetch('/api/settings');
    if (!freshRes.ok) throw new Error('reload HTTP ' + freshRes.status);
    const saved = await freshRes.json();
    document.getElementById('modal-body').innerHTML = renderSettingsModal(saved);
    settingsTab('schedule');
    const newStatus = document.getElementById('settingsSaveStatus');
    if (newStatus) {
      const labels = operatorText();
      if (restartRequired) {
        newStatus.innerHTML = escapeHtml(labels.saved + new Date().toLocaleTimeString()) + escapeHtml(labels.restartRequiredSaved) + '<code>' + escapeHtml(DASHBOARD_RESTART_COMMAND) + '</code> <button type="button" class="fo-copy-btn" onclick="copyDashboardRestartCommand()">' + escapeHtml(labels.copyCommand) + '</button><span class="fo-copy-status" id="dashboardRestartCopyStatusSaved" aria-live="polite"></span>';
      } else {
        newStatus.textContent = labels.saved + new Date().toLocaleTimeString();
      }
    }
  } catch (e) {
    if (status) status.textContent = operatorText().saveFailed + e.message;
  }
}

async function copyDashboardRestartCommand() {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(DASHBOARD_RESTART_COMMAND);
    } else {
      const text = document.createElement('textarea');
      text.value = DASHBOARD_RESTART_COMMAND;
      document.body.appendChild(text);
      text.select();
      document.execCommand('copy');
      document.body.removeChild(text);
    }
    document.querySelectorAll('#dashboardRestartCopyStatus, #dashboardRestartCopyStatusSaved').forEach(el => {
      el.textContent = foundationText().copied;
    });
  } catch (e) {
    document.querySelectorAll('#dashboardRestartCopyStatus, #dashboardRestartCopyStatusSaved').forEach(el => {
      el.textContent = foundationText().copyFailed;
    });
  }
}

function collectGeneralSettingsFromModal() {
  return {
    appName: document.getElementById('setGeneralAppName')?.value || 'Actanara',
    environment: document.getElementById('setGeneralEnvironment')?.value || 'local',
    timezone: document.getElementById('setGeneralTimezone')?.value || 'Asia/Hong_Kong',
    locale: document.getElementById('setGeneralLocale')?.value || 'zh-CN',
    workspaceRoot: document.getElementById('setGeneralWorkspaceRoot')?.value || '',
    tmpWorkspace: document.getElementById('setGeneralTmpWorkspace')?.value || '',
  };
}

function collectDashboardSettingsFromModal() {
  return {
    projectRoot: document.getElementById('setDashboardProjectRoot')?.value || '',
    pythonExecutable: document.getElementById('setDashboardPython')?.value || 'python3',
    appDir: document.getElementById('setDashboardAppDir')?.value || '',
    host: document.getElementById('setDashboardHost')?.value || '127.0.0.1',
    port: Number(document.getElementById('setDashboardPort')?.value || 3036),
    healthPath: document.getElementById('setDashboardHealthPath')?.value || '/health',
    logsDir: document.getElementById('setDashboardLogsDir')?.value || '',
    serviceLabel: document.getElementById('setDashboardServiceLabel')?.value || 'com.actanara.dashboard',
    watchdogLabel: document.getElementById('setDashboardWatchdogLabel')?.value || 'com.actanara.dashboard.watchdog',
  };
}

function collectPathSettingsFromModal() {
  const paths = {};
  document.querySelectorAll('[data-settings-path-group][data-settings-path-key]').forEach(input => {
    const group = input.dataset.settingsPathGroup;
    const key = input.dataset.settingsPathKey;
    if (!group || !key) return;
    if (group === 'runtime' && key === 'actanaraHome') return;
    if (!paths[group]) paths[group] = {};
    paths[group][key] = input.value || '';
  });
  return paths;
}

function collectRuntimeSourceSettingsFromModal() {
  const runtimeSources = {};
  document.querySelectorAll('[data-runtime-source-key]').forEach(input => {
    runtimeSources[input.dataset.runtimeSourceKey] = input.value || 'foundation';
  });
  return runtimeSources;
}

function collectExternalToolSettingsFromModal() {
  const externalTools = {};
  document.querySelectorAll('[data-external-tool][data-external-key]').forEach(input => {
    const tool = input.dataset.externalTool;
    const key = input.dataset.externalKey;
    if (!tool || !key) return;
    if (!externalTools[tool]) externalTools[tool] = {};
    if (input.dataset.externalType === 'list') {
      externalTools[tool][key] = String(input.value || '').split('\n').map(item => item.trim()).filter(Boolean);
    } else {
      externalTools[tool][key] = input.value || '';
    }
  });
  return externalTools;
}

function collectPipelineSettingsFromModal() {
  const stepTimeouts = {};
  document.querySelectorAll('[data-pipeline-step-timeout]').forEach(input => {
    stepTimeouts[input.dataset.pipelineStepTimeout] = Number(input.value || 1);
  });
  return {
    stableCommand: document.getElementById('setPipelineStableCommand')?.value || 'python advanced/pipeline/run_daily_pipeline.py [YYYY-MM-DD]',
    pythonExecutable: document.getElementById('setPipelinePython')?.value || 'python3',
    workingDirectory: document.getElementById('setPipelineWorkingDirectory')?.value || '',
    dailyDateArgument: document.getElementById('setPipelineDateArgument')?.value || 'YYYY-MM-DD',
    skipFinalRagEnv: document.getElementById('setPipelineSkipFinalRagEnv')?.value || 'ACTANARA_PIPELINE_SKIP_FINAL_RAG',
    thinkingMode: document.getElementById('setPipelineThinkingMode')?.value || 'off',
    stepTimeoutSeconds: Number(document.getElementById('setPipelineStepTimeoutSeconds')?.value || 1800),
    stepTimeouts,
    totalWatchdogSeconds: Number(document.getElementById('setPipelineTotalWatchdogSeconds')?.value || 7200),
  };
}

function collectLlmProviderSettingsFromModal() {
  const providerName = document.getElementById('llmProviderName')?.value || '';
  const custom = providerName === 'custom';
  const payload = {
    mode: custom ? 'custom' : 'preset',
    provider: providerName,
    endpoint: custom ? (document.getElementById('llmProviderEndpoint')?.value || '') : '',
    model: custom ? (document.getElementById('llmProviderModel')?.value || '') : (document.getElementById('llmProviderModelSelect')?.value || ''),
    api: custom ? (document.getElementById('llmProviderApi')?.value || 'openai-compatible') : '',
    contextWindow: custom ? (document.getElementById('llmProviderContextWindow')?.value || '') : '',
    maxTokens: custom ? (document.getElementById('llmProviderMaxTokens')?.value || '') : '',
    pipelineConcurrency: document.getElementById('llmPipelineConcurrency')?.value || '3',
    timeoutSeconds: document.getElementById('llmProviderTimeoutSeconds')?.value || '300',
    pipelineGateMode: document.getElementById('llmPipelineGateMode')?.value || 'auto',
    apiKey: document.getElementById('llmProviderApiKey')?.value || '',
  };
  if (payload.pipelineGateMode === 'manual') {
    payload.pipelineGateTokens = document.getElementById('llmPipelineGateTokens')?.value || '30000';
  }
  return payload;
}

async function refreshRagStatus() {
  const labels = ragUiText();
  const panel = document.getElementById('ragStatusPanel');
  if (panel) panel.innerHTML = '<div class="settings-note">' + escapeHtml(labels.refreshStatus) + '</div>';
  try {
    const res = await fetch('/api/rag/status?probe=true');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const status = await res.json();
    window._lastRagStatus = status;
    const rag = collectRagSettingsFromModal();
    const pane = document.getElementById('ragSearchSettings');
    if (pane) pane.innerHTML = renderRagSettings(rag, status);
  } catch (e) {
    if (panel) panel.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.refreshFailed) + escapeHtml(e.message) + '</div>';
  }
}

async function saveRagSettingsPanel() {
  const labels = ragUiText();
  const status = document.getElementById('ragActionStatus');
  if (status) status.textContent = labels.savingSettings;
  try {
    const payload = collectRagSettingsFromModal();
    const res = await fetch('/api/rag/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rag: payload})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    window._lastRagSettings = data.rag || payload;
    window._lastRagStatus = data.status || window._lastRagStatus || {};
    const pane = document.getElementById('ragSearchSettings');
    if (pane) pane.innerHTML = renderRagSettings(window._lastRagSettings, window._lastRagStatus);
    const refreshedStatus = document.getElementById('ragActionStatus');
    if (refreshedStatus) refreshedStatus.textContent = labels.savedSettings + new Date().toLocaleTimeString();
    await loadRagSearchPage();
  } catch (e) {
    if (status) status.textContent = labels.saveFailed + e.message;
  }
}

function collectRagSettingsFromModal() {
  const previous = window._lastRagSettings || {};
  const previousEmbedding = previous.embedding || {};
  const previousRetrieval = previous.retrieval || {};
  const enabled = previous.enabled !== false && (previous.mode || 'v2') !== 'disabled';
  const embeddingMode = previousEmbedding.mode || previousEmbedding.provider || 'local';
  const providerId = previousEmbedding.providerId || embeddingMode;
  const payload = {
    enabled,
    mode: enabled ? 'v2' : 'disabled',
    embedding: {
      mode: embeddingMode,
      provider: embeddingMode,
      providerId,
      model: previousEmbedding.model || 'intfloat/multilingual-e5-small',
    },
    retrieval: {
      topK: Number(document.getElementById('setRagTopK')?.value || previousRetrieval.topK || 8),
      recencyHalfLifeDays: Number(document.getElementById('setRagHalfLife')?.value || previousRetrieval.recencyHalfLifeDays || 7),
    },
    server: {
      enabled,
    },
    indexing: {
      enabled,
      defaultFullRebuild: false,
    },
  };
  const device = previousEmbedding.device;
  if (device) payload.embedding.device = device;
  const endpoint = previousEmbedding.endpoint;
  const apiKeyEnv = previousEmbedding.apiKeyEnv;
  const previousSecretRef = previousEmbedding.secretRef || {};
  const secretBackend = previousSecretRef.backend;
  const secretService = previousSecretRef.service;
  const secretAccount = previousSecretRef.account;
  if (embeddingMode === 'cloud') {
    payload.embedding.mode = 'cloud';
    payload.embedding.provider = 'cloud';
    payload.embedding.providerId = providerId && providerId !== 'local' ? providerId : 'cloud';
    payload.embedding.endpoint = endpoint || '';
    payload.embedding.apiKeyEnv = apiKeyEnv || 'NOVA_RAG_CLOUD_API_KEY';
    if (secretBackend || secretAccount) {
      payload.embedding.secretRef = {
        backend: secretBackend || 'process-env',
        service: secretService || 'actanara',
        account: secretAccount || 'NOVA_RAG_CLOUD_API_KEY',
      };
    }
  }
  return payload;
}

async function ragOperatorAction(action) {
  const labels = ragUiText();
  const status = document.getElementById('ragActionStatus');
  const rag = collectRagSettingsFromModal();
  if ((action === 'server/start' || action === 'index/run') && (rag.enabled === false || rag.mode === 'disabled')) {
    if (status) status.textContent = labels.disabledNeedEnable;
    return;
  }
  let body = {};
  if (action === 'server/start' || action === 'server/stop') {
    const confirmationText = action === 'server/start' ? 'START ACTANARA RAG SERVER' : 'STOP ACTANARA RAG SERVER';
    const typed = prompt(labels.confirmationPrompt + confirmationText);
    if (typed !== confirmationText) {
      if (status) status.textContent = labels.confirmationMismatch;
      return;
    }
    body.confirmationText = confirmationText;
  }
  if (status) status.innerHTML = '<div class="wr-loading" style="padding:6px 0"><div class="wr-spinner"></div><span>' + escapeHtml(labels.executing(action)) + '</span></div>';
  try {
    const res = await fetch('/api/rag/' + action, {method: 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + res.status));
    }
    const data = await res.json();
    if (status) {
      const jobId = data.jobId || (data.job || {}).id || '';
      status.textContent = data.status + ': ' + (jobId ? labels.queuedJob + jobId : (data.reason || data.action || action));
    }
    if (action === 'index/run') refreshBackgroundTaskButton();
  } catch (e) {
    if (status) status.textContent = labels.actionFailed + e.message;
  }
}

async function runRagProductionSync() {
  const labels = ragUiText();
  const status = document.getElementById('ragActionStatus');
  const confirmationText = 'SYNC ACTANARA RAG';
  if (RAG_PRODUCTION_SYNC_BUSY) return;
  const typed = prompt(labels.productionSyncConfirmationPrompt + confirmationText);
  if (typed !== confirmationText) {
    if (status) status.textContent = labels.productionSyncConfirmationMismatch;
    return;
  }
  RAG_PRODUCTION_SYNC_BUSY = true;
  const buttons = Array.from(document.querySelectorAll('button[onclick="runRagProductionSync()"]'));
  buttons.forEach(button => { button.disabled = true; });
  if (status) status.innerHTML = '<div class="wr-loading" style="padding:6px 0"><div class="wr-spinner"></div><span>' + escapeHtml(labels.executing(labels.productionSync)) + '</span></div>';
  try {
    const res = await fetch('/api/rag/sync/run', {method: 'POST', headers:{'Content-Type':'application/json'}, body: '{}'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (data.accepted === false) throw new Error(data.reason || data.status || 'RAG sync was not accepted');
    if (status) status.textContent = labels.productionSyncQueued + (data.jobId || (data.job || {}).id || 'queued');
    await refreshBackgroundTaskButton();
  } catch (e) {
    if (status) status.textContent = labels.productionSyncFailed + e.message;
  } finally {
    RAG_PRODUCTION_SYNC_BUSY = false;
    buttons.forEach(button => { button.disabled = false; });
  }
}

async function loadRagCoverage() {
  const labels = ragUiText();
  const panel = document.getElementById('ragCoveragePanel');
  if (!panel) return;
  panel.style.display = 'block';
  panel.innerHTML = '<div class="settings-note">' + escapeHtml(labels.loadingCoverage) + '</div>';
  try {
    const res = await fetch('/api/rag/coverage');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const summary = data.summary || {};
    const missing = summary.missingConfiguredSourceSets || [];
    const rows = (data.sourceSets || []).map(item => {
      const cls = item.coverageStatus === 'covered' ? 'ok' : (item.coverageStatus === 'optional-missing' ? '' : 'warn');
      return '<span class="settings-runtime-chip ' + cls + '">' + escapeHtml(item.sourceSet) + '=' + escapeHtml(item.coverageStatus || 'unknown') + ' · ' + Number(item.indexedChunkCount || 0).toLocaleString() + '</span>';
    }).join('');
    panel.innerHTML =
      '<div class="settings-runtime-line"><b>' + escapeHtml(labels.coverage) + '</b> active=' + escapeHtml(data.activeRunId || '—') +
      ' · sourceSets=' + (summary.indexedSourceSetCount || 0) + '/' + (summary.configuredSourceSetCount || 0) +
      ' · chunks=' + Number(summary.indexedChunkCount || 0).toLocaleString() + '</div>' +
      '<div class="settings-runtime-line"><b>' + escapeHtml(labels.path) + '</b> <code>' + escapeHtml((data.paths || {}).filteredDialoguePattern || '') + '</code></div>' +
      (missing.length ? '<div class="fo-job-error">' + escapeHtml(labels.missing) + escapeHtml(missing.join(', ')) + '</div>' : '<div class="settings-note">' + escapeHtml(labels.allCovered) + '</div>') +
      '<div class="settings-runtime-flags">' + rows + '</div>';
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.coverageFailed) + escapeHtml(e.message) + '</div>';
  }
}

async function loadRagEval() {
  const labels = ragUiText();
  const panel = document.getElementById('ragEvalPanel');
  if (!panel) return;
  panel.style.display = 'block';
  panel.innerHTML = '<div class="settings-note">' + escapeHtml(labels.runningEval) + '</div>';
  try {
    const res = await fetch('/api/rag/eval/latest');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const rows = (data.cases || []).map(item => {
      const cls = item.passed ? 'ok' : 'warn';
      return '<div class="settings-runtime-line"><span class="settings-runtime-chip ' + cls + '">' + escapeHtml(item.status || 'unknown') + '</span> ' +
        '<b>' + escapeHtml(item.id || '') + '</b> · top=' + escapeHtml(((item.top || {}).sourceSet || '—')) +
        ' · ' + escapeHtml(((item.top || {}).lifecycle || '—')) + '</div>';
    }).join('');
    panel.innerHTML =
      '<div class="settings-runtime-line"><b>' + escapeHtml(labels.retrievalEval) + '</b> ' + escapeHtml(data.status || 'unknown') +
      ' · pass=' + (data.passedCount || 0) + '/' + (data.caseCount || 0) +
      ' · rate=' + Math.round((data.passRate || 0) * 100) + '%</div>' + rows;
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.evalFailed) + escapeHtml(e.message) + '</div>';
  }
}

async function runRagSearch() {
  const labels = ragUiText();
  const box = document.getElementById('ragSearchResults');
  const query = document.getElementById('ragSearchQuery')?.value || '';
  if (box) box.textContent = labels.searching;
  try {
    const res = await fetch('/api/rag/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, topK: Number(document.getElementById('setRagTopK')?.value || 8)})
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const rows = (data.results || []).map(item => '<div class="settings-runtime-line"><b>' + escapeHtml(item.id || item.score || '') + '</b> ' + escapeHtml(item.textPreview || item.text || item.score || '') + '</div>').join('');
    if (box) box.innerHTML = rows || '<div class="settings-note">' + escapeHtml(labels.noResults) + '</div>';
  } catch (e) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.searchFailed) + escapeHtml(e.message) + '</div>';
  }
}

async function loadRagSearchPage() {
  await ensureDashboardLanguageProfile();
  const labels = ragUiText();
  const panel = document.getElementById('ragSearchStatus');
  const settingsPanel = document.getElementById('ragSearchSettings');
  if (panel) panel.innerHTML = '<div class="settings-note">' + escapeHtml(labels.readingStatus) + '</div>';
  if (settingsPanel) settingsPanel.innerHTML = '<div class="settings-note">' + escapeHtml(labels.readingSettings) + '</div>';
  try {
    const settingsRes = await fetch('/api/rag/settings');
    if (!settingsRes.ok) throw new Error('settings HTTP ' + settingsRes.status);
    const settingsPayload = await settingsRes.json();
    window._lastRagSettings = settingsPayload.rag || {};
    const res = await fetch('/api/rag/status?probe=true');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    window._lastRagSearchStatus = data;
    window._lastRagStatus = data;
    if (settingsPanel) settingsPanel.innerHTML = renderRagSettings(window._lastRagSettings, data);
    renderRagSearchStatus(data);
  } catch (e) {
    if (panel) panel.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.statusReadFailed) + escapeHtml(e.message) + '</div>';
    if (settingsPanel) settingsPanel.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.settingsReadFailed) + escapeHtml(e.message) + '</div>';
  }
}

function renderRagSearchStatus(status) {
  const labels = ragUiText();
  const panel = document.getElementById('ragSearchStatus');
  if (!panel) return;
  const server = status.server || {};
  const v2 = status.v2 || {};
  const profile = status.profile || {};
  const configuredProfile = profile.configured || {};
  const activeProfile = profile.active || {};
  const serverCls = server.healthy ? 'positive' : 'negative';
  const productEnabled = status.productEnabled !== false;
  const disabledReason = status.disabledReason;
  const migrationRequired = Boolean(profile.migrationRequired || profile.mismatch);
  const migrateButton = document.getElementById('ragProfileMigrateBtn');
  const startButton = document.getElementById('ragStartServerBtn');
  const stopButton = document.getElementById('ragStopServerBtn');
  if (migrateButton) migrateButton.disabled = false;
  if (startButton) startButton.disabled = !productEnabled || server.healthy;
  if (stopButton) stopButton.disabled = !server.healthy && !server.running;
  updateNovaRagPowerButton(status);
  const cards = [
    {label: labels.product, value: productEnabled ? labels.enabled : labels.disabled, cls: productEnabled ? 'positive' : 'negative'},
    {label: labels.search, value: status.searchAvailable ? labels.available : labels.unavailable, cls: status.searchAvailable ? 'positive' : 'negative'},
    {label: labels.server, value: server.healthy ? labels.healthy : labels.offline, cls: serverCls},
    {label: labels.ready, value: String(Boolean(status.ready)), cls: status.ready ? 'positive' : 'negative'},
    {label: labels.mode, value: status.mode || '—', cls: status.mode && status.mode !== 'disabled' ? 'positive' : 'negative'},
    {label: labels.configuredProfile, value: ragProfileLabel(configuredProfile), cls: migrationRequired ? 'negative' : 'positive'},
    {label: labels.activeProfile, value: activeProfile && activeProfile.model ? ragProfileLabel(activeProfile) : labels.none, cls: activeProfile && activeProfile.model ? 'positive' : 'negative'},
    {label: labels.activeRun, value: v2.lastBuildRunId || (status.activeIndex || {}).manifestStatus || '—', cls: v2.lastBuildRunId ? 'neutral' : 'negative'},
    {label: labels.chunks, value: Number(v2.chunkCount || 0).toLocaleString(), cls: Number(v2.chunkCount || 0) > 0 ? 'neutral' : 'negative'},
    {label: labels.documents, value: Number(v2.documentCount || 0).toLocaleString(), cls: Number(v2.documentCount || 0) > 0 ? 'neutral' : 'negative'},
  ];
  panel.innerHTML =
    '<div class="rag-status-grid">' + cards.map(card =>
      '<div class="rag-status-card ' + escapeHtml(card.cls || '') + '">' +
        '<span class="rag-status-label">' + escapeHtml(card.label) + '</span>' +
        '<b class="rag-status-value">' + escapeHtml(card.value) + '</b>' +
      '</div>'
    ).join('') + '</div>' +
    (disabledReason ? '<div class="settings-note">' + escapeHtml(labels.globalDisabled) + escapeHtml(disabledReason) + '</div>' : '') +
    (migrationRequired ? '<div class="fo-job-error">' + escapeHtml(labels.migrationRequired) + '</div>' : '') +
    (v2.dimensionMismatch ? '<div class="fo-job-error">dimensionMismatch=true</div>' : '') +
    (!status.searchAvailable && productEnabled ? '<div class="settings-note">' + escapeHtml(labels.searchUnavailableHint) + '</div>' : '');
}

function openRagProfileMigrationModal(options) {
  const labels = ragUiText();
  options = options || {};
  const initMode = Boolean(options.init);
  const status = window._lastRagSearchStatus || window._lastRagStatus || {};
  const profile = (status.profile || {}).configured || {};
  const language = (status.settings || {}).language_profile || dashboardLanguageProfile();
  const sourceRoot = (status.settings || {}).diary_source_root || '';
  const title = initMode ? labels.migrationInitTitle : labels.migrationTitle;
  const actionLabel = initMode ? labels.migrationInitAction : labels.migrationAction;
  const note = initMode ? labels.migrationInitNote : labels.migrationNote;
  openModal(title,
    '<div class="settings-section">' +
      '<div class="settings-section-title">' + escapeHtml(labels.targetProfile) + '</div>' +
      '<div class="settings-note">' + escapeHtml(note) + '</div>' +
      (sourceRoot ? '<div class="settings-runtime-line"><b>' + escapeHtml(labels.sourceRootOverride) + '</b> <code>' + escapeHtml(sourceRoot) + '</code></div>' : '') +
      '<input id="ragMigrationInitMode" type="hidden" value="' + (initMode ? '1' : '0') + '">' +
      '<div class="settings-row"><label>Mode</label><select id="ragMigrationMode" onchange="updateRagMigrationFieldVisibility()"><option value="local">local</option><option value="cloud">cloud</option></select></div>' +
      '<div id="ragMigrationProviderIdRow" class="settings-row"><label>Provider ID</label><input id="ragMigrationProviderId" value="' + escapeHtml(profile.providerId || profile.mode || 'local') + '"></div>' +
      '<div class="settings-row"><label>Language</label><select id="ragMigrationLanguage" onchange="updateRagMigrationModelOptions()"><option value="zh">zh</option><option value="en">en</option><option value="mixed">mixed</option></select></div>' +
      '<div class="settings-row"><label>Model</label><select id="ragMigrationModel" onchange="updateRagMigrationDimensionFromModel()">' + novaRagModelOptionHtml(language, profile.model || '') + '</select></div>' +
      '<div class="settings-row"><label>Dimension</label><input id="ragMigrationDimension" type="number" min="1" readonly value=""></div>' +
      '<div id="ragMigrationEndpointRow" class="settings-row"><label>Cloud endpoint</label><input id="ragMigrationEndpoint" placeholder="' + escapeHtml(labels.cloudEndpointPlaceholder) + '"></div>' +
      '<div id="ragMigrationApiKeyEnvRow" class="settings-row"><label>API key env</label><input id="ragMigrationApiKeyEnv" value="NOVA_RAG_CLOUD_API_KEY"></div>' +
      '<div id="ragMigrationConfirmationRow" class="settings-row"><label>' + escapeHtml(labels.confirmationPhrase) + '</label><input id="ragMigrationConfirmation" placeholder="' + escapeHtml(initMode ? labels.initializationConfirmation : 'MIGRATE RAG PROFILE') + '"></div>' +
      '<button type="button" class="wr-export-btn secondary" onclick="previewRagProfileMigration()">' + escapeHtml(labels.migrationPreviewAction) + '</button> ' +
      '<button type="button" class="wr-export-btn" onclick="submitRagProfileMigration()">' + escapeHtml(actionLabel) + '</button> ' +
      '<button type="button" class="wr-export-btn secondary" onclick="openBackgroundTasksModal()">' + escapeHtml(labels.viewBackgroundTasks) + '</button>' +
      '<div id="ragMigrationStatus" class="settings-note"></div>' +
    '</div>');
  const modeEl = document.getElementById('ragMigrationMode');
  const langEl = document.getElementById('ragMigrationLanguage');
  if (modeEl) modeEl.value = profile.mode || 'local';
  if (langEl) langEl.value = language;
  updateRagMigrationModelOptions();
  updateRagMigrationFieldVisibility();
}

function collectRagProfileMigrationPayload() {
  const initMode = document.getElementById('ragMigrationInitMode')?.value === '1';
  const mode = document.getElementById('ragMigrationMode')?.value || 'local';
  const providerId = mode === 'cloud' ? (document.getElementById('ragMigrationProviderId')?.value || 'cloud') : 'local';
  const model = document.getElementById('ragMigrationModel')?.value || '';
  const dimension = Number(document.getElementById('ragMigrationDimension')?.value || 0);
  const endpoint = mode === 'cloud' ? (document.getElementById('ragMigrationEndpoint')?.value || '') : '';
  const apiKeyEnv = mode === 'cloud' ? (document.getElementById('ragMigrationApiKeyEnv')?.value || 'NOVA_RAG_CLOUD_API_KEY') : 'NOVA_RAG_CLOUD_API_KEY';
  const languageProfile = document.getElementById('ragMigrationLanguage')?.value || dashboardLanguageProfile();
  const confirmationText = document.getElementById('ragMigrationConfirmation')?.value || '';
  return {
    initMode,
    confirmationText,
    autoPromote: initMode,
    targetProfile: {mode, providerId, model, dimension, endpoint, apiKeyEnv, languageProfile},
  };
}

async function previewRagProfileMigration() {
  const labels = ragUiText();
  const box = document.getElementById('ragMigrationStatus');
  try {
    const payload = collectRagProfileMigrationPayload();
    const res = await fetch('/api/rag/profile/migrate/plan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    const steps = (data.steps || []).map(step => step.id || step.label).filter(Boolean).join(' -> ');
    const effects = (data.sideEffects || []).join(', ');
    if (box) box.innerHTML = '<b>' + escapeHtml(labels.migrationPlanPrefix) + '</b>' + escapeHtml(steps || data.status || 'planned') +
      (effects ? '<br><span>' + escapeHtml(effects) + '</span>' : '');
    return data;
  } catch (e) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.migrationPlanFailed + e.message) + '</div>';
    throw e;
  }
}

async function submitRagProfileMigration() {
  const labels = ragUiText();
  const box = document.getElementById('ragMigrationStatus');
  const submitBtn = box?.parentElement?.querySelector('button[onclick="submitRagProfileMigration()"]');
  if (submitBtn?.disabled) return;
  const payload = collectRagProfileMigrationPayload();
  const initMode = payload.initMode;
  const targetProfile = payload.targetProfile;
  const mode = targetProfile.mode;
  const confirmationText = initMode ? 'MIGRATE RAG PROFILE' : payload.confirmationText;
  if (initMode && payload.confirmationText !== labels.initializationConfirmation) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.initializationConfirmationMismatch) + '</div>';
    return;
  }
  if (box) box.textContent = initMode ? labels.submittingInitTask : labels.submittingMigrationTask;
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.dataset.originalText = submitBtn.textContent || '';
    submitBtn.textContent = initMode ? labels.initSubmitting : labels.migrationSubmitting;
  }
  try {
    await previewRagProfileMigration();
    if (initMode) {
      const current = window._lastRagSettings || {};
      const currentEmbedding = current.embedding || {};
      const settingsRes = await fetch('/api/rag/settings', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({rag: {
          enabled: true,
          mode: 'v2',
          embedding: {
            mode,
            provider: mode,
            providerId: targetProfile.providerId,
            model: targetProfile.model,
            dimension: targetProfile.dimension,
            endpoint: targetProfile.endpoint,
            apiKeyEnv: targetProfile.apiKeyEnv,
            batchSize: currentEmbedding.batchSize || 200,
            device: currentEmbedding.device || 'auto',
          },
          server: {enabled: true},
          indexing: {
            enabled: true,
            defaultFullRebuild: false,
          },
          retrieval: (current.retrieval || {topK: 8, recencyHalfLifeDays: 7}),
        }})
      });
      const settingsPayload = await settingsRes.json().catch(() => ({}));
      if (!settingsRes.ok) throw new Error(settingsPayload.error || ('settings HTTP ' + settingsRes.status));
      window._lastRagSettings = settingsPayload.rag || window._lastRagSettings || {};
      window._lastRagStatus = settingsPayload.status || window._lastRagStatus || {};
      const startBody = {confirmationText: 'START ACTANARA RAG SERVER'};
      let startRes;
      try {
        startRes = await fetch('/api/rag/server/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(startBody)
        });
      } catch (startError) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        startRes = await fetch('/api/rag/server/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(startBody)
        });
      }
      const startPayload = await startRes.json().catch(() => ({}));
      if (!startRes.ok) throw new Error(startPayload.error || ('server HTTP ' + startRes.status));
      if (startPayload.accepted === false) throw new Error(startPayload.reason || startPayload.status || 'nova-RAG server start was not accepted');
    }
    const res = await fetch('/api/rag/profile/migrate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirmationText, autoPromote: initMode, targetProfile})
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + res.status));
    }
    const data = await res.json();
    if (box) box.textContent = labels.queuedTaskPrefix + (data.jobId || 'queued');
    await refreshBackgroundTaskButton();
    await loadRagSearchPage();
  } catch (e) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml((initMode ? labels.initSubmitFailed : labels.migrationSubmitFailed) + e.message) + '</div>';
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = submitBtn.dataset.originalText || (initMode ? labels.migrationInitAction : labels.confirmMigrationFallback);
    }
  }
}

function openRagExternalSkillRegistration() {
  const labels = ragUiText();
  openModal(labels.externalSkillTitle,
    '<div class="settings-section">' +
      '<div class="settings-section-title">External Agent Memory Skill</div>' +
      '<div class="settings-note">' + escapeHtml(labels.externalSkillNote) + '</div>' +
      '<div class="settings-runtime-status">' +
        '<div class="settings-runtime-line"><b>Contract</b> GET /api/rag/external/health · GET /api/rag/external/contract · POST /api/rag/external/search</div>' +
        '<div class="settings-runtime-line"><b>Policy</b> ' + escapeHtml(labels.externalSkillPolicy) + '</div>' +
      '</div>' +
      '<div class="settings-row"><label>' + escapeHtml(labels.confirmationPhrase) + '</label><input id="ragSkillRegistrationConfirmation" placeholder="INSTALL ACTANARA RAG SKILL"></div>' +
      '<label class="settings-inline"><input type="checkbox" id="ragSkillRegistrationOverwrite"> ' + escapeHtml(labels.overwriteSkill) + '</label>' +
      '<button type="button" class="wr-export-btn" onclick="loadRagExternalSkillPlan()">' + escapeHtml(labels.refreshPlan) + '</button> ' +
      '<button type="button" class="wr-export-btn" onclick="submitRagExternalSkillRegistration()">' + escapeHtml(labels.installSkill) + '</button> ' +
      '<button type="button" class="wr-export-btn secondary" onclick="loadRagExternalContractPreview()">' + escapeHtml(labels.readContract) + '</button> ' +
      '<button type="button" class="wr-export-btn secondary" onclick="openBackgroundTasksModal()">' + escapeHtml(labels.backgroundTasks) + '</button>' +
      '<div id="ragExternalSkillPreview" class="settings-runtime-status" style="margin-top:10px">' + escapeHtml(labels.readingInstallPlan) + '</div>' +
    '</div>');
  loadRagExternalSkillPlan();
}

async function loadRagExternalSkillPlan() {
  const labels = ragUiText();
  const box = document.getElementById('ragExternalSkillPreview');
  if (box) box.textContent = labels.readingInstallPlan;
  try {
    const res = await fetch('/api/settings/external-tools/rag-skill-registration/plan');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const rows = (data.operations || []).map(op =>
      '<div class="settings-runtime-line"><b>' + escapeHtml(op.tool) + '</b> ' +
      escapeHtml(op.status) + ' · ' + escapeHtml(op.skillFile || '') + '</div>'
    ).join('');
    if (box) box.innerHTML = rows || '<div class="settings-note">' + escapeHtml(labels.noRegistrationTargets) + '</div>';
  } catch (e) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.planReadFailed + e.message) + '</div>';
  }
}

async function submitRagExternalSkillRegistration() {
  const labels = ragUiText();
  const box = document.getElementById('ragExternalSkillPreview');
  const confirmationText = document.getElementById('ragSkillRegistrationConfirmation')?.value || '';
  const overwrite = document.getElementById('ragSkillRegistrationOverwrite')?.checked || false;
  if (box) box.textContent = labels.submittingRegistration;
  try {
    const res = await fetch('/api/settings/external-tools/rag-skill-registration', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dryRun: false, overwrite, confirmationText})
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + res.status));
    }
    const data = await res.json();
    if (box) box.innerHTML = '<div class="settings-note">' + escapeHtml(labels.registrationComplete + (data.results || []).length + ' target(s)') + '</div>';
    await refreshBackgroundTaskButton();
  } catch (e) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.registrationFailed + e.message) + '</div>';
  }
}

async function loadRagExternalContractPreview() {
  const labels = ragUiText();
  const box = document.getElementById('ragExternalSkillPreview');
  if (box) box.textContent = labels.readingContract;
  try {
    const res = await fetch('/api/rag/external/contract');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (box) box.innerHTML = '<pre style="white-space:pre-wrap;margin:0">' + escapeHtml(JSON.stringify(data, null, 2)) + '</pre>';
  } catch (e) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.readFailed + e.message) + '</div>';
  }
}

async function toggleNovaRagPower() {
  const status = window._lastRagSearchStatus || window._lastRagStatus || {};
  if (status.productEnabled !== false) {
    await disableNovaRagServices();
    return;
  }
  if (!isNovaRagInitialized(status)) {
    openRagProfileMigrationModal({init: true});
    return;
  }
  await enableNovaRagServices();
}

function enabledNovaRagSettingsPayload() {
  const current = window._lastRagSettings || {};
  const embedding = current.embedding || {};
  return {
    enabled: true,
    mode: current.mode && current.mode !== 'disabled' ? current.mode : 'v2',
    embedding: {
      mode: embedding.mode || embedding.provider || 'local',
      provider: embedding.provider || embedding.mode || 'local',
      providerId: embedding.providerId || embedding.provider || embedding.mode || 'local',
      model: embedding.model || 'intfloat/multilingual-e5-small',
      dimension: embedding.dimension || 384,
      batchSize: embedding.batchSize || 200,
      device: embedding.device || 'auto',
    },
    server: {enabled: true},
    indexing: Object.assign({}, current.indexing || {}, {enabled: true, defaultFullRebuild: false}),
    retrieval: current.retrieval || {topK: 8, recencyHalfLifeDays: 7},
  };
}

async function enableNovaRagServices() {
  const t = ragUiText();
  const panel = document.getElementById('ragSearchStatus');
  const action = document.getElementById('ragActionStatus');
  setNovaRagPowerBusy(t.enabling);
  if (panel) panel.innerHTML = '<div class="settings-note">' + escapeHtml(t.enablingServer) + '</div>';
  if (action) action.textContent = '';
  try {
    const settingsRes = await fetch('/api/rag/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rag: enabledNovaRagSettingsPayload()})
    });
    const settingsPayload = await settingsRes.json().catch(() => ({}));
    if (!settingsRes.ok) throw new Error(settingsPayload.error || ('settings HTTP ' + settingsRes.status));
    window._lastRagSettings = settingsPayload.rag || window._lastRagSettings || {};
    const startRes = await fetch('/api/rag/server/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirmationText: 'START ACTANARA RAG SERVER'})
    });
    const startPayload = await startRes.json().catch(() => ({}));
    if (!startRes.ok) throw new Error(startPayload.error || ('server HTTP ' + startRes.status));
    if (startPayload.accepted === false) throw new Error(startPayload.reason || startPayload.status || 'nova-RAG server start was not accepted');
    if (action) action.textContent = startPayload.status + ': ' + (startPayload.reason || t.serverStartRequested);
    await loadRagSearchPage();
  } catch (e) {
    if (panel) panel.innerHTML = '<div class="fo-job-error">' + escapeHtml(t.startFailed + e.message) + '</div>';
    updateNovaRagPowerButton(window._lastRagStatus || {});
  }
}

async function disableNovaRagServices() {
  const t = ragUiText();
  const panel = document.getElementById('ragSearchStatus');
  const action = document.getElementById('ragActionStatus');
  setNovaRagPowerBusy(t.disabling);
  if (panel) panel.innerHTML = '<div class="settings-note">' + escapeHtml(t.disablingServer) + '</div>';
  if (action) action.textContent = '';
  try {
    const stopRes = await fetch('/api/rag/server/stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirmationText: 'STOP ACTANARA RAG SERVER'})
    });
    const stopPayload = await stopRes.json().catch(() => ({}));
    if (!stopRes.ok) throw new Error(stopPayload.error || ('server HTTP ' + stopRes.status));
    const current = window._lastRagSettings || {};
    const disabledPayload = Object.assign({}, current, {
      enabled: false,
      mode: 'disabled',
      server: Object.assign({}, current.server || {}, {enabled: false}),
      indexing: Object.assign({}, current.indexing || {}, {enabled: false, defaultFullRebuild: false}),
    });
    delete disabledPayload.languageProfile;
    const settingsRes = await fetch('/api/rag/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rag: disabledPayload})
    });
    const settingsPayload = await settingsRes.json().catch(() => ({}));
    if (!settingsRes.ok) throw new Error(settingsPayload.error || ('settings HTTP ' + settingsRes.status));
    window._lastRagSettings = settingsPayload.rag || window._lastRagSettings || {};
    if (action) action.textContent = stopPayload.status + ': ' + t.disabledDone;
    await loadRagSearchPage();
  } catch (e) {
    if (panel) panel.innerHTML = '<div class="fo-job-error">' + escapeHtml(t.stopFailed + e.message) + '</div>';
    updateNovaRagPowerButton(window._lastRagStatus || {});
  }
}

async function ragSearchEnableAndStartServer() {
  await enableNovaRagServices();
}

async function ragSearchStartServer() {
  const t = ragUiText();
  const action = document.getElementById('ragActionStatus');
  const button = document.getElementById('ragStartServerBtn');
  if (button?.disabled) return;
  if (action) action.textContent = t.startingServer;
  if (button) button.disabled = true;
  try {
    const res = await fetch('/api/rag/server/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirmationText: 'START ACTANARA RAG SERVER'})
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.error || ('server HTTP ' + res.status));
    if (payload.accepted === false) throw new Error(payload.reason || payload.status || 'nova-RAG server start was not accepted');
    if (action) action.textContent = (payload.status || 'started') + ': ' + (payload.reason || t.serverStartRequested);
    await loadRagSearchPage();
  } catch (e) {
    if (action) action.innerHTML = '<div class="fo-job-error">' + escapeHtml(t.startFailed + e.message) + '</div>';
    await loadRagSearchPage();
  }
}

async function ragSearchStopServer() {
  const t = ragUiText();
  const action = document.getElementById('ragActionStatus');
  const button = document.getElementById('ragStopServerBtn');
  if (button?.disabled) return;
  if (action) action.textContent = t.stoppingServer;
  if (button) button.disabled = true;
  try {
    const res = await fetch('/api/rag/server/stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirmationText: 'STOP ACTANARA RAG SERVER'})
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.error || ('server HTTP ' + res.status));
    if (payload.accepted === false) throw new Error(payload.reason || payload.status || 'nova-RAG server stop was not accepted');
    if (action) action.textContent = (payload.status || 'stopped') + ': ' + (payload.reason || t.serverStopRequested);
    await loadRagSearchPage();
  } catch (e) {
    if (action) action.innerHTML = '<div class="fo-job-error">' + escapeHtml(t.stopFailed + e.message) + '</div>';
    await loadRagSearchPage();
  }
}

function compactIsoDateRanges(values) {
  const dates = Array.from(new Set((values || []).map(value => String(value || '').trim()).filter(value => /^\d{4}-\d{2}-\d{2}$/.test(value)))).sort();
  const ranges = [];
  let start = null;
  let previous = null;
  const nextDay = (value) => {
    const date = new Date(value + 'T00:00:00Z');
    date.setUTCDate(date.getUTCDate() + 1);
    return date.toISOString().slice(0, 10);
  };
  dates.forEach(value => {
    if (!start) {
      start = value;
      previous = value;
      return;
    }
    if (nextDay(previous) === value) {
      previous = value;
      return;
    }
    ranges.push(start === previous ? start : start + ' ~ ' + previous);
    start = value;
    previous = value;
  });
  if (start) ranges.push(start === previous ? start : start + ' ~ ' + previous);
  return ranges;
}

async function loadRagSearchCoverage() {
  const t = ragUiText();
  const panel = document.getElementById('ragSearchCoverage');
  if (panel) panel.style.display = 'block';
  if (panel) panel.innerHTML = '<div class="settings-note">' + escapeHtml(t.loadingCoverageInfo) + '</div>';
  try {
    const res = await fetch('/api/rag/coverage');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const summary = data.summary || {};
    const dateCoverage = data.dateCoverage || {};
    const dateSummary = dateCoverage.summary || {};
    const missing = summary.missingConfiguredSourceSets || [];
    const rows = (data.sourceSets || []).map(item => {
      const cls = item.coverageStatus === 'covered' ? 'ok' : (item.coverageStatus === 'optional-missing' ? '' : 'warn');
      return '<span class="settings-runtime-chip ' + cls + '">' + escapeHtml(item.sourceSet) + '=' + escapeHtml(item.coverageStatus || 'unknown') + '</span>';
    }).join('');
    const allMissingRagDates = dateCoverage.onlyMissingRagIndexDates || [];
    const allMissingUpstreamDates = (dateCoverage.missingUpstreamDates || []).map(item => item.date).filter(Boolean);
    const missingRagDates = allMissingRagDates.slice(0, 12);
    const missingUpstreamDates = (dateCoverage.missingUpstreamDates || []).slice(0, 8);
    const missingRagRanges = compactIsoDateRanges(allMissingRagDates).slice(0, 8);
    const missingUpstreamRanges = compactIsoDateRanges(allMissingUpstreamDates).slice(0, 8);
    const upstreamDateText = missingUpstreamDates.map(item => item.date + ' (' + (item.missingUpstream || []).join(', ') + ')').join('; ');
    const dateRows = (dateCoverage.dates || []).slice(-8).map(item => {
      const cls = item.onlyMissingRagIndex ? 'warn' : (item.missingUpstream || []).length ? 'warn' : 'ok';
      return '<span class="settings-runtime-chip ' + cls + '">' + escapeHtml(item.date) + ' upstream=' + escapeHtml(item.upstreamStatus || 'unknown') + ' rag=' + escapeHtml(item.ragIndexStatus || 'unknown') + '</span>';
    }).join('');
    const recommendations = [
      dateSummary.recommendRagSync ? t.recommendRagSync : '',
      dateSummary.recommendDailyPipelineOrFoundationMaterialization ? t.recommendUpstreamBackfill : ''
    ].filter(Boolean).join(' ');
    const dateActions = [
      missingRagDates.length ? '<button type="button" class="wr-export-btn" onclick="runRagProductionSync()">' + escapeHtml(t.runProductionRagSync) + '</button>' : '',
      missingUpstreamDates.length ? '<button type="button" class="wr-export-btn" onclick="openHistoryBackfillModal()">' + escapeHtml(t.openFoundationBackfill) + '</button>' : '',
      missingUpstreamDates.length ? '<button type="button" class="wr-export-btn secondary" onclick="showPage(\'foundation-ops\')">' + escapeHtml(t.openFoundationOps) + '</button>' : ''
    ].filter(Boolean).join(' ');
    panel.innerHTML =
      '<div class="settings-runtime-line" style="justify-content:space-between;gap:12px"><b>' + escapeHtml(t.coverage) + '</b>' +
      '<button type="button" class="wr-export-btn secondary" onclick="closeRagSearchCoverage()">' + escapeHtml(t.closePanel) + '</button></div>' +
      '<div class="settings-runtime-line"><b>' + escapeHtml(t.coverage) + '</b> active=' + escapeHtml(data.activeRunId || '—') +
      ' · sourceSets=' + (summary.indexedSourceSetCount || 0) + '/' + (summary.configuredSourceSetCount || 0) +
      ' · chunks=' + Number(summary.indexedChunkCount || 0).toLocaleString() + '</div>' +
      (missing.length ? '<div class="fo-job-error">' + escapeHtml(t.missing + missing.join(', ')) + '</div>' : '<div class="settings-note">' + escapeHtml(t.allCovered) + '</div>') +
      '<div class="settings-runtime-flags">' + rows + '</div>' +
      '<div class="settings-runtime-line"><b>' + escapeHtml(t.dateCoverage) + '</b> dates=' + escapeHtml(dateCoverage.dateCount || 0) +
      ' · complete=' + escapeHtml(dateSummary.completeUpstreamDateCount || 0) +
      ' · onlyRagMissing=' + escapeHtml(dateSummary.onlyMissingRagIndexDateCount || 0) +
      ' · upstreamGaps=' + escapeHtml(dateSummary.missingUpstreamDateCount || 0) + '</div>' +
      (missingRagRanges.length ? '<div class="settings-note">' + escapeHtml(t.ragIndexDateRanges + missingRagRanges.join(', ')) + '</div>' : '') +
      (missingRagDates.length ? '<div class="fo-job-error">' + escapeHtml(t.missingRagIndexDates + missingRagDates.join(', ')) + '</div>' : '') +
      (missingUpstreamRanges.length ? '<div class="settings-note">' + escapeHtml(t.upstreamDateRanges + missingUpstreamRanges.join(', ')) + '</div>' : '') +
      (missingUpstreamDates.length ? '<div class="fo-job-error">' + escapeHtml(t.missingUpstreamDates + upstreamDateText) + '</div>' : '') +
      (recommendations ? '<div class="settings-note">' + escapeHtml(recommendations) + '</div>' : '') +
      (dateActions ? '<div class="settings-actions">' + dateActions + '</div>' : '') +
      (dateRows ? '<div class="settings-runtime-flags">' + dateRows + '</div>' : '');
  } catch (e) {
    if (panel) panel.innerHTML = '<div class="fo-job-error">' + escapeHtml(t.coverageReadFailed + e.message) + '</div>';
  }
}

function closeRagSearchCoverage() {
  const panel = document.getElementById('ragSearchCoverage');
  if (!panel) return;
  panel.style.display = 'none';
  panel.innerHTML = '';
}

async function runRagPageSearch() {
  const t = ragUiText();
  const box = document.getElementById('ragPageSearchResults');
  const query = document.getElementById('ragPageSearchQuery')?.value || '';
  const topK = Number(document.getElementById('ragPageSearchTopK')?.value || 8);
  const project = document.getElementById('ragPageSearchProject')?.value || '';
  const sourceSets = _csvValues(document.getElementById('ragPageSearchSourceSets')?.value || '');
  const lifecycle = _csvValues(document.getElementById('ragPageSearchLifecycle')?.value || '');
  if (!query.trim()) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(t.searchQueryRequired) + '</div>';
    return;
  }
  if (box) box.innerHTML = '<div class="settings-note">' + escapeHtml(t.searching) + '</div>';
  try {
    const payload = {query, topK, includeGovernance: true};
    if (project.trim()) payload.project = project.trim();
    if (sourceSets.length) payload.sourceSets = sourceSets;
    if (lifecycle.length) payload.lifecycle = lifecycle;
    const res = await fetch('/api/rag/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (data.available === false) {
      if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(t.searchUnavailable + (data.reason || 'server unavailable')) + '</div>';
      await loadRagSearchPage();
      return;
    }
    const rows = (data.results || []).map(renderRagSearchResult).join('');
    if (box) box.innerHTML = rows || '<div class="settings-note">' + escapeHtml(t.noResults) + '</div>';
  } catch (e) {
    if (box) box.innerHTML = '<div class="fo-job-error">' + escapeHtml(t.searchFailed + e.message) + '</div>';
  }
}

function renderRagSearchResult(item) {
  const governance = item.governance || {};
  const score = item.score == null ? '—' : Number(item.score).toFixed(4);
  const preview = item.textPreview || item.preview || item.text || '';
  return '<div class="rag-result">' +
    '<div class="rag-result-head"><b>' + escapeHtml(item.sourceSet || item.id || 'memory') + '</b><span>score=' + escapeHtml(score) + '</span></div>' +
    '<div class="rag-result-meta">' +
    escapeHtml([item.date, item.project, item.workType, governance.lifecycle].filter(Boolean).join(' · ')) +
    '</div>' +
    '<div class="rag-result-text">' + escapeHtml(preview) + '</div>' +
    (item.sourcePath ? '<code>' + escapeHtml(item.sourcePath) + '</code>' : '') +
    '</div>';
}

function _csvValues(value) {
  return String(value || '').split(',').map(v => v.trim()).filter(Boolean);
}

async function loadSystemTimerPreview() {
  const labels = operatorText();
  const panel = document.getElementById('systemTimerPreview');
  if (!panel) return;
  panel.style.display = 'block';
  panel.innerHTML = '<div class="wr-loading" style="padding:12px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingSystemTimerPreview) + '</span></div>';
  try {
    const res = await fetch('/api/settings/scheduler/system-timer/preview');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    panel.innerHTML = renderSystemTimerPreview(await res.json());
  } catch (e) {
    panel.innerHTML = '<div class="fo-job-error" style="padding:12px">' + escapeHtml(labels.previewFailed + e.message) + '</div>';
  }
}

function renderSystemTimerPreview(data) {
  if (!data.supported) {
    return '<div class="fo-job-error" style="padding:12px">' + escapeHtml(data.error || 'unsupported provider') + '</div>';
  }
  const jobs = data.jobs || [];
  const jobHtml = jobs.map(job => {
    return '<div class="settings-timer-job"><b>' + escapeHtml(job.kind) + ' · ' + escapeHtml(job.time) + '</b>' +
      '<code>Label: ' + escapeHtml(job.label) + '</code>' +
      '<code>Plist: ' + escapeHtml(job.plistPath) + '</code>' +
      '<code>' + escapeHtml((job.programArguments || []).join(' ')) + '</code></div>';
  }).join('');
  return '<div class="settings-note" style="padding:12px;margin:0">Provider: ' + escapeHtml(data.provider) + ' · Registered: ' + (data.registered ? 'yes' : 'no') + '</div>' + jobHtml;
}

async function installSystemTimer() {
  const labels = operatorText();
  const confirmationText = 'INSTALL ACTANARA SCHEDULER';
  const typed = prompt(labels.installTimerPrompt + confirmationText);
  if (typed !== confirmationText) {
    const panel = document.getElementById('systemTimerPreview');
    if (panel) {
      panel.style.display = 'block';
      panel.innerHTML = '<div class="fo-job-error" style="padding:12px">' + escapeHtml(labels.installCancelledMismatch) + '</div>';
    }
    return;
  }
  const panel = document.getElementById('systemTimerPreview');
  if (panel) {
    panel.style.display = 'block';
    panel.innerHTML = '<div class="wr-loading" style="padding:12px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.installingSystemTimer) + '</span></div>';
  }
  try {
    const res = await fetch('/api/settings/scheduler/system-timer/install', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({confirmationText})
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + res.status));
    }
    const data = await res.json();
    if (panel) panel.innerHTML = '<div class="settings-note" style="padding:12px;margin:0">' + escapeHtml(labels.installedJobs((data.installed || []).length)) + escapeHtml(data.backupDir || 'none') + '</div>';
  } catch (e) {
    if (panel) panel.innerHTML = '<div class="fo-job-error" style="padding:12px">' + escapeHtml(labels.installFailed + e.message) + '</div>';
  }
}

async function uninstallSystemTimer() {
  const labels = operatorText();
  const confirmationText = 'UNINSTALL ACTANARA SCHEDULER';
  const typed = prompt(labels.uninstallTimerPrompt + confirmationText);
  if (typed !== confirmationText) {
    const panel = document.getElementById('systemTimerPreview');
    if (panel) {
      panel.style.display = 'block';
      panel.innerHTML = '<div class="fo-job-error" style="padding:12px">' + escapeHtml(labels.uninstallCancelledMismatch) + '</div>';
    }
    return;
  }
  const panel = document.getElementById('systemTimerPreview');
  if (panel) {
    panel.style.display = 'block';
    panel.innerHTML = '<div class="wr-loading" style="padding:12px"><div class="wr-spinner"></div><span>' + escapeHtml(labels.uninstallingSystemTimer) + '</span></div>';
  }
  try {
    const res = await fetch('/api/settings/scheduler/system-timer/uninstall', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({confirmationText})
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + res.status));
    }
    const data = await res.json();
    if (panel) panel.innerHTML = '<div class="settings-note" style="padding:12px;margin:0">' + escapeHtml(labels.uninstalledJobs((data.removed || []).length)) + escapeHtml(data.backupDir || 'none') + '</div>';
  } catch (e) {
    if (panel) panel.innerHTML = '<div class="fo-job-error" style="padding:12px">' + escapeHtml(labels.uninstallFailed + e.message) + '</div>';
  }
}

async function openLlmProviderModal() {
  const labels = llmUiText();
  openModal(labels.title, '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingProvider) + '</span></div>');
  try {
    const res = await fetch('/api/llm-provider');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const provider = await res.json();
    document.getElementById('modal-body').innerHTML = renderLlmProviderModal(provider);
  } catch (e) {
    document.getElementById('modal-body').innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.readProviderFailed + e.message) + '</div>';
  }
}

function renderLlmProviderModal(provider) {
  return renderLlmProviderSettings(provider, true);
}

function renderLlmProviderSettings(provider, includeActions) {
  const labels = llmUiText();
  const catalog = provider.catalog || [];
  const rawProvider = provider.provider || provider.presetProvider || 'custom';
  const selectedProvider = catalog.find(item => item.id === rawProvider) || {id:'custom', models:[]};
  const currentProvider = selectedProvider.id || rawProvider;
  const selectedModel = (selectedProvider.models || []).find(item => item.id === provider.model) || (selectedProvider.models || [])[0] || {};
  const custom = currentProvider === 'custom' || provider.mode === 'custom';
  const providerOptions = catalog.map(item => {
    const disabled = item.enabled === false ? 'disabled' : '';
    const suffix = item.enabled === false ? ` - ${providerStatusLabel(item.status)}` : '';
    return `<option value="${escapeHtml(item.id)}" ${item.id === currentProvider ? 'selected' : ''} ${disabled}>${escapeHtml((item.name || item.id) + suffix)}</option>`;
  }).join('');
  const modelOptions = (selectedProvider.models || []).map(item => `<option value="${escapeHtml(item.id)}" ${item.id === (provider.model || selectedModel.id) ? 'selected' : ''}>${escapeHtml(item.name || item.id)}</option>`).join('');
  const contextWindow = custom ? (provider.contextWindow || '') : (selectedModel.contextWindow || provider.contextWindow || '');
  const maxTokens = custom ? (provider.maxTokens || '') : (selectedModel.maxTokens || provider.maxTokens || '');
  const pipelineConcurrency = provider.pipelineConcurrency || 3;
  const timeoutSeconds = provider.timeoutSeconds || 300;
  const autoPipelineGateTokens = provider.autoPipelineGateTokens || llmAutoGateTokens(contextWindow);
  const pipelineGateMode = provider.pipelineGateMode || 'auto';
  const pipelineGateTokens = provider.pipelineGateTokens || autoPipelineGateTokens;
  const gateDrift = Number(pipelineGateTokens) !== Number(autoPipelineGateTokens);
  const apiType = custom ? (provider.api || selectedProvider.api || 'openai-compatible') : (selectedProvider.api || provider.api || 'openai-compatible');
  const endpointValue = custom ? (provider.endpoint || selectedProvider.endpoint || '') : (selectedProvider.endpoint || provider.endpoint || '');
  const providerNote = providerCatalogNote(selectedProvider);
  const gateNote = pipelineGateMode === 'manual'
    ? labels.manualOverride(escapeHtml(autoPipelineGateTokens), gateDrift)
    : labels.autoGate(escapeHtml(autoPipelineGateTokens));
  const actions = includeActions ? `
    <div class="settings-actions">
      <span class="settings-status" id="llmSaveStatus"></span>
      <button class="wr-export-btn" onclick="closeModal()">${escapeHtml(labels.cancel)}</button>
      <button class="wr-export-btn" onclick="testLlmProviderModal()">${escapeHtml(labels.testAvailability)}</button>
      <button class="wr-export-btn" onclick="saveLlmProviderModal()">${escapeHtml(labels.saveProvider)}</button>
    </div>` : '';
  return `
    <div class="settings-section">
      <div class="settings-row"><label>${escapeHtml(labels.provider)}</label><select id="llmProviderName" onchange="llmProviderCatalogChanged()">
        ${providerOptions}
      </select></div>
      <div class="settings-row llm-preset-row" style="${custom ? 'display:none' : ''}"><label>${escapeHtml(labels.model)}</label><select id="llmProviderModelSelect" onchange="llmProviderModelChanged()">${modelOptions}</select></div>
      <div class="settings-row"><label>${escapeHtml(labels.apiType)}</label><select id="llmProviderApi" ${custom ? '' : 'disabled'}>
        <option value="openai-compatible" ${apiType === 'openai-compatible' ? 'selected' : ''}>OpenAI compatible</option>
        <option value="anthropic-messages" ${apiType === 'anthropic-messages' ? 'selected' : ''}>Anthropic Messages</option>
      </select></div>
      <div class="settings-row"><label>Endpoint</label><input id="llmProviderEndpoint" value="${escapeHtml(endpointValue)}" ${custom ? '' : 'readonly'}></div>
      <div class="settings-row llm-custom-row" style="${custom ? '' : 'display:none'}"><label>${escapeHtml(labels.model)}</label><input id="llmProviderModel" value="${escapeHtml(provider.model || selectedModel.id || '')}"></div>
      <div class="settings-row"><label>Context</label><input id="llmProviderContextWindow" value="${escapeHtml(contextWindow)}" ${custom ? '' : 'readonly'}></div>
      <div class="settings-row"><label>Max Tokens</label><input id="llmProviderMaxTokens" value="${escapeHtml(maxTokens)}" ${custom ? '' : 'readonly'}></div>
      <div class="settings-row"><label>${escapeHtml(labels.pipelineConcurrency)}</label><input id="llmPipelineConcurrency" type="number" min="1" max="8" step="1" value="${escapeHtml(pipelineConcurrency)}"></div>
      <div class="settings-row"><label>${escapeHtml(labels.requestTimeout)}</label><input id="llmProviderTimeoutSeconds" type="number" min="30" max="900" step="30" value="${escapeHtml(timeoutSeconds)}"></div>
      <div class="settings-row"><label>Pipeline Gate Tokens</label><input id="llmPipelineGateTokens" type="number" min="1000" step="100" value="${escapeHtml(pipelineGateTokens)}" oninput="llmPipelineGateEdited()"></div>
      <div class="settings-row"><label>${escapeHtml(labels.autoSuggestion)}</label><div class="settings-inline-control"><input id="llmPipelineGateMode" type="hidden" value="${escapeHtml(pipelineGateMode)}"><span id="llmPipelineGateAutoValue">${escapeHtml(autoPipelineGateTokens)}</span><button type="button" class="wr-export-btn" onclick="llmUseAutoPipelineGate()">${escapeHtml(labels.useAutoValue)}</button></div></div>
      <div class="settings-row"><label>${escapeHtml(labels.apiKey)}</label><input id="llmProviderApiKey" type="password" value="${escapeHtml(provider.apiKey || '')}" placeholder="${provider.hasApiKey ? labels.savedKeepBlank : labels.notConfigured}"></div>
      <div class="settings-note" id="llmProviderTestResult">${escapeHtml(labels.testBeforeSave)}</div>
      <div class="settings-note" id="llmProviderCatalogNote">${providerNote}</div>
      <div class="settings-note" id="llmPipelineGateNote">${gateNote}</div>
      <div class="settings-note">${escapeHtml(labels.regularModeNote)}</div>
      <script type="application/json" id="llmProviderCatalogData">${escapeHtml(JSON.stringify(catalog))}</script>
    </div>${actions}`;
}

function llmProviderCatalog() {
  try {
    return JSON.parse(document.getElementById('llmProviderCatalogData')?.textContent || '[]');
  } catch (_) {
    return [];
  }
}

function providerStatusLabel(status) {
  const labels = llmUiText();
  if (status === 'needs_transport') return labels.statusNeedsTransport;
  if (status === 'auth_only_or_local') return labels.statusAuthLocal;
  if (status === 'custom') return labels.statusCustom;
  return labels.statusAvailable;
}

function llmAutoGateTokens(contextWindow) {
  const parsed = Number.parseInt(contextWindow, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) return 30000;
  return Math.max(1000, Math.min(Math.floor(parsed * 0.15), 80000));
}

function providerCatalogNote(provider) {
  if (!provider) return '';
  const status = providerStatusLabel(provider.status);
  const endpoint = provider.endpoint ? `Endpoint: ${escapeHtml(provider.endpoint)}` : llmUiText().endpointMissing;
  const api = provider.api && provider.api !== 'custom' ? `API: ${escapeHtml(provider.api)}` : '';
  const note = provider.note ? escapeHtml(provider.note) : '';
  return [status, endpoint, api, note].filter(Boolean).join(' · ');
}

function llmProviderCatalogChanged() {
  const catalog = llmProviderCatalog();
  const providerId = document.getElementById('llmProviderName')?.value || 'custom';
  const provider = catalog.find(item => item.id === providerId) || {id:'custom', models:[]};
  const custom = providerId === 'custom';
  document.querySelectorAll('.llm-preset-row').forEach(el => { el.style.display = custom ? 'none' : ''; });
  document.querySelectorAll('.llm-custom-row').forEach(el => { el.style.display = custom ? '' : 'none'; });
  const api = document.getElementById('llmProviderApi');
  if (api) {
    api.disabled = !custom;
    api.value = provider.api && provider.api !== 'custom' ? provider.api : 'openai-compatible';
  }
  const endpoint = document.getElementById('llmProviderEndpoint');
  if (endpoint) {
    endpoint.readOnly = !custom;
    endpoint.value = custom ? '' : (provider.endpoint || '');
  }
  const modelSelect = document.getElementById('llmProviderModelSelect');
  if (modelSelect) {
    modelSelect.innerHTML = (provider.models || []).map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name || item.id)}</option>`).join('');
  }
  const note = document.getElementById('llmProviderCatalogNote');
  if (note) note.innerHTML = providerCatalogNote(provider);
  llmProviderModelChanged();
}

function llmProviderModelChanged() {
  const catalog = llmProviderCatalog();
  const providerId = document.getElementById('llmProviderName')?.value || 'custom';
  const modelId = document.getElementById('llmProviderModelSelect')?.value || '';
  const provider = catalog.find(item => item.id === providerId) || {};
  const model = (provider.models || []).find(item => item.id === modelId) || {};
  const custom = providerId === 'custom';
  const context = document.getElementById('llmProviderContextWindow');
  const maxTokens = document.getElementById('llmProviderMaxTokens');
  const gate = document.getElementById('llmPipelineGateTokens');
  const gateMode = document.getElementById('llmPipelineGateMode');
  const autoValue = document.getElementById('llmPipelineGateAutoValue');
  const gateNote = document.getElementById('llmPipelineGateNote');
  if (!custom) {
    if (context) context.value = model.contextWindow || '';
    if (maxTokens) maxTokens.value = model.maxTokens || '';
  }
  const autoGate = llmAutoGateTokens((custom ? context?.value : model.contextWindow) || '');
  if (autoValue) autoValue.textContent = String(autoGate);
  if (gate && gateMode && gateMode.value !== 'manual') {
    gate.value = String(autoGate);
    gateMode.value = 'auto';
  }
  if (gateNote && gate && gateMode) {
    const drift = Number(gate.value || 0) !== Number(autoGate);
    const labels = llmUiText();
    gateNote.textContent = gateMode.value === 'manual'
      ? labels.manualOverride(autoGate, drift)
      : labels.autoGate(autoGate);
  }
}

function llmPipelineGateEdited() {
  const mode = document.getElementById('llmPipelineGateMode');
  if (mode) mode.value = 'manual';
  llmProviderModelChanged();
}

function llmUseAutoPipelineGate() {
  const mode = document.getElementById('llmPipelineGateMode');
  if (mode) mode.value = 'auto';
  llmProviderModelChanged();
}

async function saveLlmProviderModal() {
  const labels = llmUiText();
  const status = document.getElementById('llmSaveStatus');
  if (status) status.textContent = labels.saving;
  const payload = collectLlmProviderSettingsFromModal();
  try {
    const res = await fetch('/api/llm-provider', {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + res.status));
    }
    const saved = await res.json();
    document.getElementById('modal-body').innerHTML = renderLlmProviderModal(saved);
    const newStatus = document.getElementById('llmSaveStatus');
    if (newStatus) newStatus.textContent = labels.saved + new Date().toLocaleTimeString();
  } catch (e) {
    if (status) status.textContent = labels.saveFailed + e.message;
  }
}

async function testLlmProviderModal() {
  const labels = llmUiText();
  const resultEl = document.getElementById('llmProviderTestResult');
  const payload = collectLlmProviderSettingsFromModal();
  if (resultEl) resultEl.textContent = labels.testing;
  try {
    const res = await fetch('/api/llm-provider/test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + res.status));
    }
    const data = await res.json();
    if (!resultEl) return;
    const target = [data.provider, data.model, data.api].filter(Boolean).join(' / ');
    if (data.ok) {
      resultEl.textContent = labels.testPassed + target + ' · ' + (data.latencyMs || 0) + 'ms' + (data.responsePreview ? ' · ' + data.responsePreview : '');
    } else {
      resultEl.textContent = labels.testFailedFull + (data.error || data.status || 'unknown') + (target ? ' · ' + target : '');
    }
  } catch (e) {
    if (resultEl) resultEl.textContent = labels.testFailed + e.message;
  }
}

// Legacy hard-coded card-detail snapshots are omitted from the public static demo.

// ── Charts ──
const PURPLE = '#533afd';
const PURPLE_LIGHT = '#b9b9f9';
const PURPLE_MID = '#665efd';
const NAVY = '#061b31';
const SLATE = '#64748b';
const RUBY = '#ea2261';
const SUCCESS = '#15be53';
const AMBER = '#f59e0b';
const MAGENTA = '#f96bee';

Chart.defaults.font.family = "-apple-system, system-ui, sans-serif";
Chart.defaults.color = SLATE;

function makeBarChart(id, labels, data, colors) {
  return new Chart(document.getElementById(id), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors || PURPLE,
        borderRadius: 4,
        borderSkipped: false,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: SLATE, font: { size: 11 } } },
        y: { grid: { color: '#f0f4f8' }, ticks: { color: SLATE, font: { size: 11 } } }
      }
    }
  });
}

function makeDoughnutChart(id, labels, data, colors) {
  return new Chart(document.getElementById(id), {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: colors, borderWidth: 0 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '65%',
      plugins: {
        legend: {
          position: 'right',
          labels: { boxWidth: 10, padding: 8, font: { size: 11 }, color: NAVY }
        }
      }
    }
  });
}

function makeLineChart(id, labels, datasets) {
  return new Chart(document.getElementById(id), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { display: true, position: 'top', labels: { boxWidth: 12, padding: 12, font: { size: 11 } } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: SLATE, font: { size: 11 } } },
        y: { grid: { color: '#f0f4f8' }, ticks: { color: SLATE, font: { size: 11 } } }
      }
    }
  });
}

/* ═══ TokenClock — 当日实时总览数据 ═══ */
const TC_TOOL_COLORS = {
  'OpenClaw': '#FF6B35', 'Claude Code': '#D97706',
  'Gemini CLI': '#3B82F6', 'Codex': '#10B981', 'Hermes': '#F59E0B',
  'OpenCode': '#0EA5A4', 'Antigravity': '#6366F1', 'Cursor': '#7C3AED'
};

function renderTokenClock(data) {
  const labels = dashboardShellText();
  const el = id => document.getElementById(id);

  // Date subtitle
  if (data.today) {
    const d = new Date(data.today.replace(/-/g, '/'));
    const y = d.getFullYear();
    const m = d.getMonth() + 1;
    const day = d.getDate();
    if (el('overviewDate')) el('overviewDate').textContent = dashboardLanguageProfile() === 'en'
      ? y + '-' + String(m).padStart(2, '0') + '-' + String(day).padStart(2, '0')
      : y + ' 年 ' + m + ' 月 ' + day + ' 日';
  }

  // KPI cards
  if (el('tcTotalTokens')) el('tcTotalTokens').textContent = formatTokens(data.totalTokens || 0);
  if (el('tcRateEmoji')) el('tcRateEmoji').textContent = data.rateEmoji || '🔥';
  if (el('tcTotalMessages')) el('tcTotalMessages').textContent = (data.totalMessages || 0).toLocaleString();

  const cacheRate = data.overallCacheRate || 0;
  const cacheEl = el('tcCacheRate');
  if (cacheEl) {
    cacheEl.textContent = cacheRate.toFixed(1) + '%';
    cacheEl.className = 'tc-kpi-value ' + (cacheRate >= 70 ? 'cache-good' : cacheRate >= 40 ? 'cache-warn' : 'cache-bad');
  }

  // Hourly rate: find current hour's tokens
  const now = new Date();
  const currentHour = String(now.getHours()).padStart(2, '0');
  let currentHourTokens = 0;
  (data.hourlyTimeline || []).forEach(h => {
    if (h.hour === currentHour) currentHourTokens = h.tokens;
  });
  if (el('tcHourlyRate')) el('tcHourlyRate').textContent = formatTokens(currentHourTokens) + '/h';

  // Active tools
  const tools = data.tools || [];
  const activeCount = tools.filter(t => t.isActive).length;
  if (el('tcActiveTools')) el('tcActiveTools').textContent = activeCount + '/' + tools.length;

  // Timeline heatmap
  const timelineByHour = new Map((data.hourlyTimeline || []).map(h => [String(h.hour).padStart(2, '0'), h]));
  const timeline = Array.from({length: 24}, (_, idx) => {
    const hour = String((idx + 4) % 24).padStart(2, '0');
    return timelineByHour.get(hour) || {hour, tokens: 0, messages: 0};
  });
  const maxTokens = Math.max(...timeline.map(h => h.tokens), 1);
  let tlHtml = '';
  for (const h of timeline) {
    const pct = (h.tokens / maxTokens) * 100;
    const barH = Math.max(pct, 3);
    const intensity = h.tokens / maxTokens;
    const bg = h.tokens === 0
      ? 'rgba(83,58,253,0.06)'
      : 'rgba(83,58,253,' + (0.15 + intensity * 0.7).toFixed(2) + ')';
    const isNow = h.hour === currentHour;
    tlHtml += '<div class="tc-timeline-cell' + (isNow ? ' now' : '') + '">' +
      '<div class="tc-tip">' + h.hour + ':00 — ' + formatTokens(h.tokens) + ' ' + escapeHtml(labels.tokenUnit) + ' / ' + h.messages + ' ' + escapeHtml(labels.messageUnitShort) + '</div>' +
      '<div class="tc-timeline-bar" style="height:' + barH + 'px;background:' + bg + '"></div>' +
      '<div class="tc-timeline-label">' + h.hour + '</div>' +
      '</div>';
  }
  if (el('tcTimeline')) el('tcTimeline').innerHTML = tlHtml;

  // Tools cards
  let toolsHtml = '';
  for (const t of tools) {
    const color = TC_TOOL_COLORS[t.name] || 'var(--purple)';
    const activeClass = t.isActive ? ' active' : '';
    const dotClass = t.isActive ? 'on' : 'off';
    const cacheColor = t.cacheRate >= 70 ? '#34d399' : t.cacheRate >= 40 ? '#fbbf24' : '#f87171';
    toolsHtml += '<div class="tc-tool-card' + activeClass + '" data-tool="' + t.name + '">' +
      '<div class="tc-tool-header">' +
        '<span class="tc-tool-dot ' + dotClass + '"></span>' +
        '<span class="tc-tool-name">' + t.name + '</span>' +
        '<span class="tc-tool-emoji">' + t.emoji + '</span>' +
      '</div>' +
      '<div class="tc-tool-stat"><span>Token</span><span class="tc-tool-stat-val">' + formatTokens(t.tokens) + '</span></div>' +
      '<div class="tc-tool-stat"><span>' + escapeHtml(labels.messages) + '</span><span class="tc-tool-stat-val">' + t.messages + '</span></div>' +
      '<div class="tc-tool-stat"><span>' + escapeHtml(labels.cacheHit) + '</span><span class="tc-tool-stat-val">' + t.cacheRate.toFixed(1) + '%</span></div>' +
      '<div class="tc-tool-stat"><span>' + escapeHtml(labels.currentRate) + '</span><span class="tc-tool-stat-val">' + formatTokens(t.hourlyTokens) + '/h</span></div>' +
      '<div class="tc-tool-bar-track"><div class="tc-tool-bar-fill" style="width:' + t.cacheRate + '%;background:' + cacheColor + '"></div></div>' +
    '</div>';
  }
  if (el('tcToolsGrid')) el('tcToolsGrid').innerHTML = toolsHtml;

  renderRealtimeWorkspaces(data.workspaceUsage || [], data.timestamp);
}

function renderRealtimeWorkspaces(workspaces, updatedAt) {
  const labels = dashboardShellText();
  const container = document.getElementById('agentTableContainer');
  if (!container) return;
  if (!workspaces.length) {
    container.innerHTML = '<div class="chart-card" style="padding:30px;text-align:center;color:var(--slate)">' + escapeHtml(labels.noWorkspaceUsageToday) + '</div>';
    return;
  }
  let html = '<div class="tc-workspace-shell"><table class="data-table tc-workspace-table"><thead><tr>' +
    '<th>' + escapeHtml(labels.agentWorkspaceHeader) + '</th><th>' + escapeHtml(labels.tool) + '</th><th>' + escapeHtml(labels.todayTokens) + '</th><th>' + escapeHtml(labels.currentHour) + '</th><th>' + escapeHtml(labels.messages) + '</th><th>' + escapeHtml(labels.status) + '</th>' +
    '</tr></thead><tbody>';
  for (const item of workspaces) {
    const badge = item.isActive ? 'badge-success' : 'badge-slate';
    const status = item.isActive ? labels.active : labels.usedToday;
    html += '<tr>' +
      '<td><span class="tc-workspace-project"><span class="tc-workspace-emoji">' + escapeHtml(item.emoji || '') + '</span>' + escapeHtml(item.name) + '</span></td>' +
      '<td class="tc-workspace-tool">' + escapeHtml(item.tool || '') + '</td>' +
      '<td class="tc-workspace-tokens">' + formatTokens(item.tokens || 0) + '</td>' +
      '<td>' + formatTokens(item.hourlyTokens || 0) + '/h</td>' +
      '<td>' + (item.messages || 0).toLocaleString() + '</td>' +
      '<td><span class="badge ' + badge + '">' + status + '</span></td>' +
      '</tr>';
  }
  html += '</tbody></table></div>';
  container.innerHTML = html;
  const updateEl = document.getElementById('agentUpdateTime');
  if (updateEl) {
    const dt = updatedAt ? new Date(updatedAt) : new Date();
    updateEl.textContent = labels.realtimeUpdatedAt + dt.toLocaleTimeString(dashboardLanguageProfile() === 'en' ? 'en-US' : 'zh-CN', { hour12: false });
  }
}

function fetchTokenClock() {
  fetch('/api/token-clock').then(r => r.json()).then(data => {
    renderTokenClock(data);
  }).catch(err => {
    console.error('TokenClock fetch error:', err);
  });
}

/* ═══ AI Assets Page — Data Fetch & Render ═══ */
let _aaCharts = { trend: null, model: null, tool: null };
let _aaState = { data: null, skillTab: 'global' };
let _aaLoading = false;

function aaFmtTokens(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function aaDestroyCharts() {
  if (_aaCharts.trend) { _aaCharts.trend.destroy(); _aaCharts.trend = null; }
  if (_aaCharts.model) { _aaCharts.model.destroy(); _aaCharts.model = null; }
  if (_aaCharts.tool) { _aaCharts.tool.destroy(); _aaCharts.tool = null; }
}

function renderAACharts(d) {
  const labels = dashboardText();
  const assetLabels = aiAssetsText();
  const tools = d.tools || [];
  if (!tools.length) return;

  // Heatmap for 30-day trend
  renderHeatmap(d.trend30d || []);

  // Agent / workspace consumption bar chart
  const workspaces = d.workspaceUsage || [];
  const toolEl = document.getElementById('aaToolChart');
  if (toolEl && workspaces.length) {
    toolEl.parentElement.style.height = Math.max(320, workspaces.length * 31) + 'px';
    _aaCharts.tool = new Chart(toolEl, {
      type: 'bar',
      data: {
        labels: workspaces.map(w => (w.emoji || '') + ' ' + w.name),
        datasets: [{
          label: assetLabels.allTimeTokens,
          data: workspaces.map(w => w.tokens),
          backgroundColor: workspaces.map(w => aaToolColor(w.tool)),
          borderRadius: 6,
          borderSkipped: false,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, indexAxis: 'y',
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => aaFmtTokens(ctx.raw) + ' ' + assetLabels.tokenUnit + ' · ' + (workspaces[ctx.dataIndex]?.tool || '') } } },
        scales: {
          x: { grid: { color: '#f0f4f8' }, ticks: { color: '#64748b', callback: v => aaFmtTokens(v) } },
          y: { grid: { display: false }, ticks: { color: '#061b31', font: { weight: '500' } } }
        }
      }
    });
  }

  // Model usage bar chart
  const models = (d.models || []).slice(0, 10);
  const modelEl = document.getElementById('aaModelChart');
  if (modelEl && models.length) {
    modelEl.parentElement.style.height = '360px';
    const modelColors = ['#533afd','#8B5CF6','#D97706','#10B981','#F59E0B','#EF4444','#3B82F6','#EC4899'];
    _aaCharts.model = new Chart(modelEl, {
      type: 'bar',
      data: {
        labels: models.map(m => m.name),
        datasets: [{
          label: labels.cumulativeTokens,
          data: models.map(m => m.tokens),
          backgroundColor: models.map((_, i) => modelColors[i % modelColors.length]),
          borderRadius: 6,
          borderSkipped: false,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, indexAxis: 'y',
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => aaFmtTokens(ctx.raw) + ' ' + assetLabels.tokenUnit } } },
        scales: {
          x: { grid: { color: '#f0f4f8' }, ticks: { color: '#64748b', callback: v => aaFmtTokens(v) } },
          y: {
            afterFit: scale => { scale.width = Math.max(scale.width, 168); },
            grid: { display: false },
            ticks: {
              color: '#061b31',
              font: { weight: '500', size: 11 },
              callback: function(value) {
                const name = this.getLabelForValue(value);
                return name.length > 24 ? name.slice(0, 22) + '...' : name;
              }
            }
          }
        }
      }
    });
  }
}

const _SLOTS = ['上午', '下午', '晚上', '凌晨'];
const _SLOT_EMOJI = ['☀️', '🌤️', '🌆', '🌙'];
const _SLOT_RANGE = ['04-12', '12-18', '18-24', '00-04'];

function renderHeatmap(trend30d, targetId = 'aaHeatmapWrap', showLegend = false) {
  const labels = dashboardText();
  const assetLabels = aiAssetsText();
  const slotLabels = labels.timeSlotLabels || _SLOTS;
  const wrap = document.getElementById(targetId);
  if (!wrap || !trend30d.length) return;

  let allVals = [];
  trend30d.forEach(d => { const s = d.slots || {}; _SLOTS.forEach(k => { const v = s[k] || 0; if (v > 0) allVals.push(v); }); });
  const maxT = allVals.length ? Math.max(...allVals) : 1;
  const colors = ['#ebedf0','#c6e48b','#7bc96f','#4ac33e','#239a3b','#196127','#0e4429','#003d19'];

  // Compute quantile thresholds for better color distribution
  const sorted = allVals.filter(v => v > 0).sort((a, b) => a - b);
  const thresholds = [];
  for (let i = 1; i < colors.length; i++) {
    const idx = Math.floor(sorted.length * (i / colors.length));
    thresholds.push(sorted[Math.min(idx, sorted.length - 1)]);
  }

  function colorIdx(v) {
    if (v === 0) return 0;
    for (let i = thresholds.length - 1; i >= 0; i--) {
      if (v >= thresholds[i]) return i + 1;
    }
    return 1;
  }

  // Build rows: one row per time slot
  let html = '<div class="aa-heatmap-table">';
  for (let si = 0; si < _SLOTS.length; si++) {
    const slotKey = _SLOTS[si];
    const slotLabel = slotLabels[si] || slotKey;
    html += '<div class="aa-heatmap-row">';
    html += '<div class="aa-heatmap-row-label">' + _SLOT_EMOJI[si] + escapeHtml(slotLabel) + ' (' + _SLOT_RANGE[si] + ')</div>';
    html += '<div class="aa-heatmap-cells">';
    for (let di = 0; di < trend30d.length; di++) {
      const d = trend30d[di];
      const v = (d.slots || {})[slotKey] || (d.slots || {})[slotLabel] || 0;
      const ci = colorIdx(v);
      const dateStr = d.date.slice(5);
      html += '<div class="aa-heatmap-cell" style="background:' + colors[ci] + '" data-tooltip="' + dateStr + ' ' + escapeHtml(slotLabel) + ': ' + aaFmtTokens(v) + ' ' + assetLabels.tokenUnit + '"></div>';
    }
    html += '</div></div>';
  }

  // Date labels row: all 30 days
  html += '<div class="aa-heatmap-row"><div class="aa-heatmap-row-label"></div>';
  html += '<div class="aa-heatmap-dates">';
  for (let di = 0; di < trend30d.length; di++) {
    html += '<div class="aa-heatmap-date">' + trend30d[di].date.slice(8) + '</div>';
  }
  html += '</div></div>';
  html += '</div>';
  if (showLegend) {
    html += '<div class="aa-heatmap-legend"><span>' + escapeHtml(labels.lowActivity) + '</span><div class="aa-heatmap-legend-scale">' +
      colors.map(color => '<span style="background:' + color + '"></span>').join('') +
      '</div><span>' + escapeHtml(labels.highActivity) + '</span></div>';
  }

  wrap.innerHTML = html;
}

function aaToolColor(name) {
  const map = { 'OpenClaw':'#FF6B35','Claude Code':'#D97706','Gemini CLI':'#8B5CF6','Codex':'#10B981','Hermes':'#F59E0B','OpenCode':'#0EA5A4','Antigravity':'#6366F1','Cursor':'#7C3AED' };
  return map[name] || '#533afd';
}

async function loadAiAssets() {
  const labels = aiAssetsText();
  if (_aaLoading) return;
  _aaLoading = true;
  const loading = document.getElementById('aiAssetsLoading');
  const content = document.getElementById('aiAssetsContent');
  const btn = document.getElementById('aiAssetsRefreshBtn');
  const timeEl = document.getElementById('aiAssetsUpdateTime');
  if (!loading || !content) { _aaLoading = false; return; }

  loading.style.display = 'flex';
  content.style.display = 'none';
  if (btn) btn.textContent = labels.loading;

  try {
    const res = await fetch('/api/ai-assets');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const d = await res.json();
    loading.style.display = 'none';
    content.style.display = 'block';
    if (btn) btn.textContent = labels.refresh;
    if (timeEl) timeEl.textContent = labels.updatedAt + new Date().toLocaleTimeString() + foundationFreshnessSuffix(d.dataFreshness && d.dataFreshness.aiAssets);
    aaDestroyCharts();
    aaRender(d);
  } catch (e) {
    loading.innerHTML = '<span style="color:var(--ruby)">❌ ' + escapeHtml(labels.loadFailed + e.message) + '</span>';
    if (btn) btn.textContent = labels.retry;
  } finally {
    _aaLoading = false;
  }
}

async function refreshAiAssetsSnapshot() {
  const labels = aiAssetsText();
  const btn = document.getElementById('aiAssetsSnapshotRefreshBtn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = labels.submitting;
  }
  try {
    const res = await fetch('/api/ai-assets/refresh', {method: 'POST'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const queued = await res.json();
    await waitForFoundationRefresh(queued.runId, status => {
      if (btn) btn.textContent = status === 'queued' ? labels.queued : labels.updating;
    });
    await loadAiAssets();
    if (btn) btn.textContent = labels.backgroundUpdate;
  } catch (e) {
    if (btn) btn.textContent = labels.retryUpdate;
    window.alert(labels.updateFailed + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function foDateString(d) {
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

function foToday() {
  const timezone = ACTANARA_DASHBOARD_TIMEZONE || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Hong_Kong';
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date()).reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
  return new Date(Number(parts.year), Number(parts.month) - 1, Number(parts.day));
}

async function loadDashboardTimezone() {
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) return;
    const settings = await res.json();
    rememberDashboardSettings(settings);
    const schedule = settings.schedule || {};
    const general = settings.general || {};
    ACTANARA_DASHBOARD_TIMEZONE = schedule.timezone || general.timezone || ACTANARA_DASHBOARD_TIMEZONE;
  } catch (e) {}
}

function foCurrentWeekStart() {
  const d = foToday();
  const day = d.getDay() || 7;
  d.setDate(d.getDate() - day + 1);
  return d;
}

function foCurrentMonthStart() {
  const d = foToday();
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function foDaysBetween(start, end) {
  const startUtc = Date.UTC(start.getFullYear(), start.getMonth(), start.getDate());
  const endUtc = Date.UTC(end.getFullYear(), end.getMonth(), end.getDate());
  return Math.max(1, Math.round((endUtc - startUtc) / 86400000) + 1);
}

function foStatusLabel(status) {
  const labels = foundationText();
  return labels.statusLabels[status] || status || labels.statusLabels.unknown;
}

function foFormatJob(job) {
  const labels = foundationText();
  const meta = job.metadata || {};
  const period = meta.periodStart ? escapeHtml(meta.periodStart + labels.dateRangeTo + (meta.periodEnd || job.business_date || '')) : escapeHtml(job.business_date || '—');
  const completed = job.completed_at ? escapeHtml(job.completed_at.replace('T', ' ').slice(0, 19)) : '—';
  const error = job.error_summary ? '<div class="fo-job-error">' + escapeHtml(job.error_summary) + '</div>' : '';
  return '<tr>' +
    '<td>#' + job.id + '</td>' +
    '<td>' + escapeHtml(meta.scope || job.trigger_type) + '</td>' +
    '<td>' + period + '</td>' +
    '<td><span class="fo-status fo-status-' + escapeHtml(job.status) + '">' + foStatusLabel(job.status) + '</span>' + error + '</td>' +
    '<td>' + escapeHtml((job.started_at || '').replace('T', ' ').slice(0, 19)) + '</td>' +
    '<td>' + completed + '</td>' +
    '</tr>';
}

function foFormatBool(value) {
  return value ? 'yes' : 'no';
}

function foFormatTime(value) {
  return value ? escapeHtml(String(value).replace('T', ' ').slice(0, 19)) : '—';
}

function foFormatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return bytes + ' B';
}

function foFormatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '—';
  const total = Math.max(0, Math.round(Number(seconds)));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    return hours + 'h ' + (minutes % 60) + 'm';
  }
  return minutes ? (minutes + 'm ' + rest + 's') : (rest + 's');
}

function foDefaultBusinessDate() {
  const d = foToday();
  d.setDate(d.getDate() - 1);
  return foDateString(d);
}

function foStatusTone(status) {
  if (status === 'ready' || status === 'complete' || status === 'completed') return 'ready';
  if (status === 'attention' || status === 'warning') return 'attention';
  if (status === 'blocked' || status === 'failed') return 'blocked';
  return 'neutral';
}

function foRenderDailyQaStat(label, value, status) {
  return '<div class="fo-qa-stat fo-qa-' + foStatusTone(status) + '">' +
    '<span>' + escapeHtml(label) + '</span>' +
    '<b>' + escapeHtml(value) + '</b>' +
    '</div>';
}

function foRenderDailyQaDocuments(documents) {
  const labels = foundationText();
  const required = documents && documents.required ? documents.required : {};
  const docLabels = labels.documents;
  return Object.keys(docLabels).map(type => {
    const item = required[type] || {};
    const missing = []
      .concat(item.missingSections || [])
      .concat((item.missingEmbeddedKeys || []).map(k => 'JSON.' + k));
    const status = item.present && missing.length === 0 ? 'ready' : 'blocked';
    return '<div class="fo-qa-row">' +
      '<span>' + docLabels[type] + '</span>' +
      '<b><span class="fo-status fo-status-' + status + '">' + (status === 'ready' ? labels.complete : labels.missing) + '</span></b>' +
      '<em>' + (missing.length ? escapeHtml(missing.join(', ')) : escapeHtml((item.sectionCount || 0) + ' sections')) + '</em>' +
      '</div>';
  }).join('');
}

function foRenderDailyQaReadiness(readiness) {
  const labels = { metrics: 'Metrics', memory: 'Memory', tasks: 'Tasks' };
  return Object.keys(labels).map(key => {
    const item = readiness && readiness[key] ? readiness[key] : {};
    const status = item.ready ? 'ready' : 'blocked';
    return '<div class="fo-qa-row">' +
      '<span>' + labels[key] + '</span>' +
      '<b><span class="fo-status fo-status-' + status + '">' + escapeHtml(item.status || 'missing') + '</span></b>' +
      '<em>' + escapeHtml(item.sourceRunId ? ('Run #' + item.sourceRunId) : (item.totalTokens ? wrFormatTokens(item.totalTokens) + ' tokens' : '—')) + '</em>' +
      '</div>';
  }).join('');
}

function foRenderDailyQaIssues(title, items) {
  if (!items || !items.length) return '';
  const labels = foundationText();
  return '<div class="fo-qa-issues"><div class="fo-qa-issues-title">' + escapeHtml(title) + '</div>' +
    items.map(item => '<div class="fo-qa-issue fo-qa-' + foStatusTone(item.severity === 'warning' ? 'attention' : 'blocked') + '">' +
      '<b>' + escapeHtml(item.title || item.key || 'issue') + '</b>' +
      '<span><strong>' + escapeHtml(item.summary || item.section || item.surface || item.failedStep || item.status || item.reportType || '') + '</strong>' +
      (item.impact ? '<small>' + escapeHtml(labels.impactPrefix) + escapeHtml(item.impact) + '</small>' : '') +
      (item.action ? '<small>' + escapeHtml(labels.actionPrefix) + escapeHtml(item.action) + '</small>' : '') +
      '<code>' + escapeHtml(item.key || '') + '</code></span>' +
      '</div>').join('') +
    '</div>';
}

function foRenderDailyQaRepairCommands(commands) {
  if (!commands || !commands.length) return '';
  const labels = foundationText();
  return '<div class="fo-qa-actions"><div class="fo-qa-section-title">' + escapeHtml(labels.repairCommands) + '</div>' +
    commands.map((item, index) => '<div class="fo-qa-command">' +
      '<div><b>' + escapeHtml(item.label || 'Command') + '</b><span>' + escapeHtml(item.actionClass || item.risk || 'manual') + '</span></div>' +
      foRenderDailyQaRepairPolicy(item.executionPolicy) +
      '<pre>' + escapeHtml(item.command || '') + '</pre>' +
      '<button type="button" class="fo-copy-btn" onclick="copyFoundationRepairCommand(' + index + ')">' + escapeHtml(labels.copyCommand) + '</button>' +
      foRenderDailyQaRepairRunControl(item, index) +
      '<span class="fo-copy-status" id="fo-copy-status-' + index + '" aria-live="polite"></span>' +
      '</div>').join('') +
    '</div>';
}

function foRenderDailyQaRepairPolicy(policy) {
  const labels = foundationText();
  if (!policy) return '<p class="fo-repair-policy">' + escapeHtml(labels.manualOnly) + '</p>';
  const state = policy.executionState || (policy.dashboardExecutable ? 'dashboard-executable' : 'manual-only');
  const details = [];
  details.push(policy.dashboardExecutable ? labels.dashboardExecutable : labels.copyCommandOnly);
  if (policy.requiresLock) details.push(labels.requiresLock);
  if (policy.requiresTypedConfirmation) details.push(labels.requiresConfirmation + (policy.confirmationPhrase || 'RUN <date>'));
  if (policy.requiresAudit) details.push(labels.requiresAudit);
  return '<p class="fo-repair-policy"><b>' + escapeHtml(state) + '</b><span>' + escapeHtml(details.join(' · ')) + '</span>' +
    (policy.reason ? '<small>' + escapeHtml(policy.reason) + '</small>' : '') +
    '</p>';
}

function foRenderDailyQaRepairRunControl(item, index) {
  const policy = item.executionPolicy || {};
  const labels = foundationText();
  if (!policy.dashboardExecutable) return '';
  const phrase = policy.confirmationPhrase || '';
  return '<div class="fo-repair-run-control">' +
    '<input id="fo-repair-confirm-' + index + '" type="text" placeholder="' + escapeHtml(phrase) + '" autocomplete="off">' +
    '<button type="button" class="wr-export-btn" id="fo-repair-run-btn-' + index + '" onclick="runFoundationRepairCommand(' + index + ')">' + escapeHtml(labels.execute) + '</button>' +
    '</div>' +
    '<div class="fo-repair-run-status" id="fo-repair-run-status-' + index + '" aria-live="polite"></div>';
}

async function copyFoundationRepairCommand(index) {
  const labels = foundationText();
  const commands = (window.FOUNDATION_REPAIR_COMMANDS || []);
  const item = commands[index];
  if (!item || !item.command) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(item.command);
    } else {
      const el = document.createElement('textarea');
      el.value = item.command;
      document.body.appendChild(el);
      el.select();
      document.execCommand('copy');
      document.body.removeChild(el);
    }
    setFoundationRepairCopyStatus(index, labels.copied);
  } catch (e) {
    setFoundationRepairCopyStatus(index, labels.copyFailed);
  }
}

function setFoundationRepairCopyStatus(index, text) {
  const labels = foundationText();
  const buttons = document.querySelectorAll('.fo-copy-btn');
  const button = buttons[index];
  if (!button) return;
  const original = button.dataset.originalText || button.textContent || labels.copyCommand;
  button.dataset.originalText = original;
  button.textContent = text;
  const status = document.getElementById('fo-copy-status-' + index);
  if (status) status.textContent = text;
  setTimeout(() => {
    button.textContent = original;
    if (status) status.textContent = '';
  }, 2200);
}

async function waitForFoundationRepairRun(runId, updateStatus) {
  const labels = foundationText();
  for (let attempt = 0; attempt < 240; attempt++) {
    const res = await fetch('/api/foundation/ops/daily-qa/repair-runs/' + runId);
    if (!res.ok) throw new Error(labels.repairStatusReadFailed + res.status);
    const run = await res.json();
    updateStatus(run);
    if (run.status === 'completed') return run;
    if (run.status === 'failed') throw new Error(run.errorSummary || labels.repairRunFailed);
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error(labels.repairStillRunning);
}

async function runFoundationRepairCommand(index) {
  const labels = foundationText();
  const commands = (window.FOUNDATION_REPAIR_COMMANDS || []);
  const item = commands[index];
  const dateInput = document.getElementById('foDailyQaDate');
  const confirmInput = document.getElementById('fo-repair-confirm-' + index);
  const btn = document.getElementById('fo-repair-run-btn-' + index);
  const status = document.getElementById('fo-repair-run-status-' + index);
  if (!item || !item.actionId || !dateInput || !dateInput.value) return;
  const confirmationText = confirmInput ? confirmInput.value.trim() : '';
  const original = btn ? btn.textContent : labels.execute;
  if (btn) {
    btn.disabled = true;
    btn.textContent = labels.submitting;
  }
  if (status) status.textContent = '';
  try {
    const res = await fetch('/api/foundation/ops/daily-qa/repair-runs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        actionId: item.actionId,
        businessDate: dateInput.value,
        confirmationText
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    const runId = data.run && data.run.id;
    if (status) status.textContent = 'Run #' + runId + ' · queued';
    await waitForFoundationRepairRun(runId, run => {
      if (btn) btn.textContent = run.status === 'queued' ? labels.queued : labels.running;
      if (status) {
        const qa = run.qaAfter && run.qaAfter.status ? (' · QA ' + run.qaAfter.status) : '';
        status.textContent = 'Run #' + run.id + ' · ' + run.status + qa;
      }
    });
    await loadFoundationDailyQa();
    await loadFoundationDailyQaOverview();
    await loadFoundationDailyPipelineSummary();
    await loadFoundationRepairRuns();
  } catch (e) {
    if (status) status.textContent = labels.executionFailed + e.message;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

function foRenderDailyQa(payload) {
  const labels = foundationText();
  if (!payload || payload.error) return '<div class="fo-job-error">' + escapeHtml(labels.dailyQaReadFailed) + escapeHtml(payload && payload.error || 'unknown') + '</div>';
  const status = payload.status || 'unknown';
  const docs = payload.documents || {};
  const ingestion = payload.foundationIngestion || {};
  const latestRun = ingestion.latestForDate || {};
  const actions = payload.nextActions || [];
  window.FOUNDATION_REPAIR_COMMANDS = payload.repairCommands || [];
  return '<div class="fo-qa-summary fo-qa-' + foStatusTone(status) + '">' +
    '<div><span>' + escapeHtml(labels.status) + '</span><b>' + escapeHtml(status) + '</b></div>' +
    '<div><span>' + escapeHtml(labels.businessDate) + '</span><b>' + escapeHtml(payload.businessDate || '—') + '</b></div>' +
    '<div><span>' + escapeHtml(labels.latestFoundationRun) + '</span><b>' + escapeHtml(latestRun.id ? ('#' + latestRun.id + ' · ' + foStatusLabel(latestRun.status)) : '—') + '</b></div>' +
    '<div><span>' + escapeHtml(labels.generatedAt) + '</span><b>' + foFormatTime(payload.generatedAt) + '</b></div>' +
    '</div>' +
    '<div class="fo-qa-stats">' +
      foRenderDailyQaStat(labels.documentsStat, (docs.count || 0) + ' / 3', docs.status) +
      foRenderDailyQaStat(labels.blockers, String((payload.blockers || []).length), (payload.blockers || []).length ? 'blocked' : 'ready') +
      foRenderDailyQaStat(labels.warnings, String((payload.warnings || []).length), (payload.warnings || []).length ? 'attention' : 'ready') +
    '</div>' +
    '<div class="fo-qa-grid">' +
      '<div><div class="fo-qa-section-title">' + escapeHtml(labels.generatedArtifacts) + '</div>' + foRenderDailyQaDocuments(docs) + '</div>' +
      '<div><div class="fo-qa-section-title">' + escapeHtml(labels.foundationInputs) + '</div>' + foRenderDailyQaReadiness(payload.diaryReadiness || {}) + '</div>' +
    '</div>' +
    foRenderDailyQaIssues('Blockers', payload.blockers || []) +
    foRenderDailyQaIssues('Warnings', payload.warnings || []) +
    (actions.length ? '<div class="fo-qa-actions"><div class="fo-qa-section-title">' + escapeHtml(labels.recommendedActions) + '</div>' + actions.map(a => '<div>' + escapeHtml(a) + '</div>').join('') + '</div>' : '') +
    foRenderDailyQaRepairCommands(payload.repairCommands || []);
}

function foRenderDailyPipelineSummary(payload) {
  const labels = foundationText();
  if (!payload || payload.error) return '<div class="fo-job-error">' + escapeHtml(labels.pipelineReadFailed) + escapeHtml(payload && payload.error || 'unknown') + '</div>';
  const latest = payload.latestRun || {};
  const materialization = payload.latestMaterializationRun || {};
  const blankInputs = payload.latestBlankInputsRun || {};
  const isBlankDay = payload.activityState === 'empty';
  const docs = payload.documents || {};
  const lessons = payload.lessons || {};
  const tasks = payload.tasks || {};
  const files = docs.files || [];
  const status = payload.status || 'unknown';
  const kpis = [
    [labels.runStatus, foStatusLabel(status), foStatusTone(status)],
    [labels.activity, isBlankDay ? labels.noActivity : labels.activeState, isBlankDay ? 'ready' : 'neutral'],
    [labels.latestRun, latest.id ? ('#' + latest.id + ' · ' + foStatusLabel(latest.status)) : '—', latest.status || status],
    [labels.duration, foFormatDuration(latest.durationSeconds || materialization.durationSeconds), latest.status || status],
    [labels.fileSize, foFormatBytes(docs.totalBytes), docs.count ? 'ready' : 'incomplete'],
    [labels.lessons, String(lessons.count || 0), lessons.count ? 'ready' : 'neutral'],
    [labels.taskUpdates, String(tasks.matchedUpdates || 0), tasks.matchedUpdates ? 'ready' : 'neutral'],
    [labels.taskCandidates, String(tasks.candidateCount || 0), tasks.candidateCount ? 'attention' : 'neutral'],
    [labels.materialization, materialization.id ? ('#' + materialization.id + ' · ' + foStatusLabel(materialization.status)) : '—', materialization.status || 'neutral'],
    [labels.blankInputs, blankInputs.id ? ('#' + blankInputs.id + ' · ' + foStatusLabel(blankInputs.status)) : '—', blankInputs.status || 'neutral'],
  ].map(item => foRenderDailyQaStat(item[0], item[1], item[2])).join('');
  const fileRows = files.length
    ? files.map(file => '<tr>' +
      '<td>' + escapeHtml(file.reportType || 'unknown') + '</td>' +
      '<td>' + escapeHtml(file.relativePath || '—') + '</td>' +
      '<td>' + foFormatBytes(file.byteSize) + '</td>' +
      '<td>' + escapeHtml(String(file.sectionCount || 0)) + '</td>' +
      '</tr>').join('')
    : '<tr><td colspan="4">' + escapeHtml(labels.noGeneratedFileProjection) + '</td></tr>';
  const taskRows = tasks.skipped
    ? '<div class="fo-empty">' + escapeHtml(labels.blankTaskSkipped) + '</div>'
    : (tasks.recentEvents || []).length
    ? (tasks.recentEvents || []).slice(0, 5).map(event => '<div class="fo-pipeline-list-row">' +
      '<b>' + escapeHtml(event.event_type || 'event') + '</b>' +
      '<span>' + escapeHtml(event.summary || '') + '</span>' +
      '</div>').join('')
    : '<div class="fo-empty">' + escapeHtml(labels.noTaskEvidence) + '</div>';
  const lessonRows = (lessons.items || []).length
    ? (lessons.items || []).slice(0, 3).map(lesson => '<div class="fo-pipeline-list-row">' +
      '<b>' + escapeHtml(lesson.agent || 'unknown') + '</b>' +
      '<span>' + escapeHtml(lesson.problem || lesson.suggestion || '') + '</span>' +
      '</div>').join('')
    : '<div class="fo-empty">' + escapeHtml(labels.noLesson) + '</div>';
  const failure = payload.latestPipelineFailure
    ? '<div class="fo-job-error">' + escapeHtml(labels.latestFailedStep) + escapeHtml(payload.latestPipelineFailure.failedStep || payload.latestPipelineFailure.reason || 'unknown') + '</div>'
    : '';
  const blankNotice = isBlankDay
    ? '<div class="fo-blank-day-note"><b>' + escapeHtml(labels.blankDayFastPath) + '</b><span>' + escapeHtml(labels.blankDayDesc) + '</span></div>'
    : '';
  return '<div class="fo-qa-summary fo-qa-' + foStatusTone(status) + '">' +
    '<div><span>' + escapeHtml(labels.businessDate) + '</span><b>' + escapeHtml(payload.businessDate || '—') + '</b></div>' +
    '<div><span>' + escapeHtml(labels.status) + '</span><b><span class="fo-status fo-status-' + foStatusTone(status) + '">' + escapeHtml(foStatusLabel(status)) + '</span></b></div>' +
    '<div><span>' + escapeHtml(labels.start) + '</span><b>' + foFormatTime(latest.startedAt || materialization.startedAt) + '</b></div>' +
    '<div><span>' + escapeHtml(labels.finish) + '</span><b>' + foFormatTime(latest.completedAt || materialization.completedAt) + '</b></div>' +
    '</div>' +
    failure +
    blankNotice +
    '<div class="fo-qa-stats fo-pipeline-stats">' + kpis + '</div>' +
    '<div class="fo-qa-grid">' +
      '<div><div class="fo-qa-section-title">' + escapeHtml(labels.taskUpdates) + '</div>' + taskRows + '</div>' +
      '<div><div class="fo-qa-section-title">' + escapeHtml(labels.lessons) + '</div>' + lessonRows + '</div>' +
    '</div>' +
    '<div class="aa-table-shell"><table class="data-table fo-table"><thead><tr><th>' + escapeHtml(labels.type) + '</th><th>' + escapeHtml(labels.file) + '</th><th>' + escapeHtml(labels.size) + '</th><th>' + escapeHtml(labels.sections) + '</th></tr></thead><tbody>' + fileRows + '</tbody></table></div>';
}

async function loadFoundationDailyPipelineSummary() {
  await ensureDashboardLanguageProfile();
  const labels = foundationText();
  const box = document.getElementById('foDailyPipelineSummary');
  const input = document.getElementById('foDailyPipelineDate');
  const qaInput = document.getElementById('foDailyQaDate');
  if (!box || !input) return;
  if (!input.value) input.value = (qaInput && qaInput.value) || foDefaultBusinessDate();
  box.innerHTML = '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingPipeline) + '</span></div>';
  try {
    const res = await fetch('/api/foundation/ops/daily-pipeline-summary?date=' + encodeURIComponent(input.value) + '&limit=30');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    box.innerHTML = foRenderDailyPipelineSummary(data);
  } catch (e) {
    box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.pipelineReadFailed) + escapeHtml(e.message) + '</div>';
  }
}

function foRenderRepairRuns(payload) {
  const labels = foundationText();
  if (!payload || payload.error) return '<div class="fo-job-error">' + escapeHtml(labels.repairAuditReadFailed) + escapeHtml(payload && payload.error || 'unknown') + '</div>';
  const runs = payload.runs || [];
  if (!runs.length) return '<div class="fo-empty">' + escapeHtml(labels.noRepairRuns) + '</div>';
  const rows = runs.slice(0, 20).map(run => {
    const status = run.status || 'unknown';
    const qaBefore = run.qaBefore && run.qaBefore.status ? run.qaBefore.status : '—';
    const qaAfter = run.qaAfter && run.qaAfter.status ? run.qaAfter.status : '—';
    return '<tr>' +
      '<td>#' + escapeHtml(String(run.id || '—')) + '</td>' +
      '<td>' + escapeHtml(run.businessDate || '—') + '</td>' +
      '<td>' + escapeHtml(run.actionId || '—') + '</td>' +
      '<td><span class="fo-status fo-status-' + foStatusTone(status) + '">' + escapeHtml(status) + '</span></td>' +
      '<td>' + escapeHtml(qaBefore + ' → ' + qaAfter) + '</td>' +
      '<td>' + foFormatDuration(runDurationSeconds(run)) + '</td>' +
      '<td>' + foFormatTime(run.completedAt || run.startedAt || run.requestedAt) + '</td>' +
      '<td>' + escapeHtml(run.errorSummary || '') + '</td>' +
      '</tr>';
  }).join('');
  return '<div class="aa-table-shell"><table class="data-table fo-table"><thead><tr><th>' + escapeHtml(labels.run) + '</th><th>' + escapeHtml(labels.date) + '</th><th>' + escapeHtml(labels.action) + '</th><th>' + escapeHtml(labels.statusHeader) + '</th><th>QA</th><th>' + escapeHtml(labels.duration) + '</th><th>' + escapeHtml(labels.updated) + '</th><th>' + escapeHtml(labels.error) + '</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
}

function runDurationSeconds(run) {
  const started = Date.parse(run && run.startedAt || '');
  const completed = Date.parse(run && run.completedAt || '');
  if (!Number.isFinite(started) || !Number.isFinite(completed)) return null;
  return Math.max(0, (completed - started) / 1000);
}

async function loadFoundationRepairRuns() {
  await ensureDashboardLanguageProfile();
  const labels = foundationText();
  const box = document.getElementById('foRepairRuns');
  if (!box) return;
  box.innerHTML = '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingRepairAudit) + '</span></div>';
  try {
    const res = await fetch('/api/foundation/ops/daily-qa/repair-runs?limit=20');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    box.innerHTML = foRenderRepairRuns(data);
  } catch (e) {
    box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.repairAuditReadFailed) + escapeHtml(e.message) + '</div>';
  }
}

function foRenderDailyQaOverview(payload) {
  const labels = foundationText();
  if (!payload || payload.error) return '<div class="fo-job-error">' + escapeHtml(labels.recentQaReadFailed) + escapeHtml(payload && payload.error || 'unknown') + '</div>';
  const counts = payload.counts || {};
  const rows = payload.rows || [];
  const summary = '<div class="fo-qa-overview-summary">' +
    '<span>' + escapeHtml(labels.recentDays(payload.days || rows.length || 0)) + '</span>' +
    '<b class="fo-qa-' + foStatusTone(payload.status) + '">' + escapeHtml(payload.status || 'unknown') + '</b>' +
    '<span>' + escapeHtml(labels.ready) + ' ' + (counts.ready || 0) + '</span>' +
    '<span>' + escapeHtml(labels.attention) + ' ' + (counts.attention || 0) + '</span>' +
    '<span>' + escapeHtml(labels.statusLabels.blocked) + ' ' + (counts.blocked || 0) + '</span>' +
    '</div>';
  const cards = rows.map(row => {
    const status = row.status || 'unknown';
    const label = String(row.businessDate || '').slice(5) || '—';
    return '<button type="button" class="fo-qa-day-card fo-qa-' + foStatusTone(status) + '" onclick="foundationLoadDailyQaDate(\'' + escapeHtml(row.businessDate || '') + '\')">' +
      '<b>' + escapeHtml(label) + '</b>' +
      '<span>' + escapeHtml(status) + '</span>' +
      '<em>' + (row.documentCount || 0) + '/3 ' + escapeHtml(labels.docs) + ' · ' + (row.foundationInputsReady || 0) + '/' + (row.foundationInputsTotal || 0) + ' ' + escapeHtml(labels.inputs) + '</em>' +
      '</button>';
  }).join('');
  return summary + '<div class="fo-qa-day-strip">' + cards + '</div>';
}

function foundationLoadDailyQaDate(dateValue) {
  const input = document.getElementById('foDailyQaDate');
  const pipelineInput = document.getElementById('foDailyPipelineDate');
  if (!input || !dateValue) return;
  input.value = dateValue;
  if (pipelineInput) pipelineInput.value = dateValue;
  loadFoundationDailyQa();
  loadFoundationDailyPipelineSummary();
}

async function loadFoundationDailyQaOverview() {
  await ensureDashboardLanguageProfile();
  const labels = foundationText();
  const box = document.getElementById('foDailyQaOverview');
  const input = document.getElementById('foDailyQaDate');
  if (!box || !input) return;
  if (!input.value) input.value = foDefaultBusinessDate();
  box.innerHTML = '<div class="fo-empty">' + escapeHtml(labels.readingRecentQa) + '</div>';
  try {
    const res = await fetch('/api/foundation/ops/daily-qa/overview?end=' + encodeURIComponent(input.value) + '&days=7&limit=20');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    box.innerHTML = foRenderDailyQaOverview(data);
  } catch (e) {
    box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.recentQaReadFailed) + escapeHtml(e.message) + '</div>';
  }
}

async function loadFoundationDailyQa() {
  await ensureDashboardLanguageProfile();
  const labels = foundationText();
  const box = document.getElementById('foDailyQa');
  const input = document.getElementById('foDailyQaDate');
  if (!box || !input) return;
  if (!input.value) input.value = foDefaultBusinessDate();
  const pipelineInput = document.getElementById('foDailyPipelineDate');
  if (pipelineInput && !pipelineInput.value) pipelineInput.value = input.value;
  box.innerHTML = '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingDailyQa) + '</span></div>';
  try {
    const res = await fetch('/api/foundation/ops/daily-qa?date=' + encodeURIComponent(input.value) + '&limit=30');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    box.innerHTML = foRenderDailyQa(data);
  } catch (e) {
    box.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.dailyQaReadFailed) + escapeHtml(e.message) + '</div>';
  }
}

function foFormatCompletenessRow(row) {
  const status = row.complete ? 'complete' : (row.status || 'missing');
  const source = row.sourceRunId ? ('Run #' + row.sourceRunId) : '—';
  const generated = row.generatedAt ? foFormatTime(row.generatedAt) : '—';
  const detail = [
    row.projectionType ? escapeHtml(row.projectionType) : '',
    row.start ? escapeHtml(row.start + (row.end ? foundationText().dateRangeTo + row.end : '')) : '',
    row.error ? '<span class="fo-job-error-inline">' + escapeHtml(row.error) + '</span>' : ''
  ].filter(Boolean).join('<br>');
  return '<tr>' +
    '<td>' + escapeHtml(row.label || row.key) + (row.optional ? '<span class="fo-chip">optional</span>' : '') + '</td>' +
    '<td><span class="fo-status fo-status-' + escapeHtml(status) + '">' + escapeHtml(status) + '</span></td>' +
    '<td>' + source + '</td>' +
    '<td>' + generated + '</td>' +
    '<td>' + (detail || '—') + '</td>' +
    '</tr>';
}

function foRenderCompleteness(matrix) {
  const labels = foundationText();
  if (!matrix || !Array.isArray(matrix.rows)) return '<div class="fo-empty">' + escapeHtml(labels.noCompletenessData) + '</div>';
  const summary = '<div class="fo-summary-line">' +
    '<b>' + escapeHtml(matrix.status || 'unknown') + '</b>' +
    '<span>' + escapeHtml(labels.required) + ' ' + (matrix.requiredComplete || 0) + '/' + (matrix.requiredTotal || 0) + '</span>' +
    '<span>' + escapeHtml(labels.total) + ' ' + (matrix.complete || 0) + '/' + (matrix.total || 0) + '</span>' +
    '</div>';
  const rows = matrix.rows.map(foFormatCompletenessRow).join('');
  return summary + '<div class="aa-table-shell"><table class="data-table fo-table"><thead><tr><th>' + escapeHtml(labels.projection) + '</th><th>' + escapeHtml(labels.status) + '</th><th>' + escapeHtml(labels.sourceRun) + '</th><th>' + escapeHtml(labels.generatedAt) + '</th><th>' + escapeHtml(labels.detail) + '</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
}

function foRenderCadence(cadence) {
  const labels = foundationText();
  cadence = cadence || {};
  const timer = cadence.systemTimer || {};
  const latest = cadence.latestRefreshJob || {};
  return [
    [labels.status, cadence.status || 'unknown'],
    [labels.dashboardScheduler, foFormatBool(cadence.scheduleEnabled)],
    [labels.schedulerLoop, foFormatBool(cadence.running)],
    [labels.systemTimer, timer.registered ? labels.registered : (timer.supported === false ? labels.unsupported : labels.notRegistered)],
    [labels.nextAggregation, foFormatTime(cadence.nextDashboardAggregationAt)],
    [labels.dailyPipeline, cadence.dailyPipelineTime || '—'],
    [labels.dashboardAggregation, cadence.dashboardAggregationTime || '—'],
    [labels.latestJob, latest.id ? ('#' + latest.id + ' · ' + foStatusLabel(latest.status)) : '—'],
    [labels.lastError, cadence.lastError || '—'],
  ].map(row => '<div class="fo-kv"><span>' + escapeHtml(row[0]) + '</span><b>' + escapeHtml(row[1]) + '</b></div>').join('');
}

async function loadFoundationOps() {
  await ensureDashboardLanguageProfile();
  const labels = aiAssetsText();
  const foundationLabels = foundationText();
  const jobsEl = document.getElementById('foJobs');
  const completenessEl = document.getElementById('foCompleteness');
  const cadenceEl = document.getElementById('foCadence');
  const runtimeEl = document.getElementById('foRuntime');
  const failedEl = document.getElementById('foLatestFailed');
  const subtitle = document.getElementById('foSubtitle');
  if (!jobsEl || !completenessEl || !cadenceEl || !runtimeEl || !failedEl) return;
  loadFoundationDailyQaOverview();
  loadFoundationDailyQa();
  loadFoundationDailyPipelineSummary();
  loadFoundationRepairRuns();
  jobsEl.innerHTML = '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingFoundationJobs) + '</span></div>';
  completenessEl.innerHTML = '<div class="wr-loading"><div class="wr-spinner"></div><span>' + escapeHtml(labels.readingCompleteness) + '</span></div>';
  cadenceEl.innerHTML = labels.loading;
  try {
    const res = await fetch('/api/foundation/ops/snapshot?limit=30');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const completeness = data.projectionCompleteness || {};
    const cadence = data.scheduledRunCadence || {};
    const runtime = completeness.runtime || {};
    completenessEl.innerHTML = foRenderCompleteness(completeness);
    cadenceEl.innerHTML = foRenderCadence(cadence);
    runtimeEl.innerHTML = [
      ['ACTANARA_HOME', runtime.actanaraHome || '—'],
      [foundationLabels.database, runtime.database || '—'],
      [foundationLabels.dbExists, runtime.databaseExists ? foundationLabels.yes : foundationLabels.no],
    ].map(row => '<div class="fo-kv"><span>' + row[0] + '</span><b>' + escapeHtml(row[1]) + '</b></div>').join('');
    const failed = cadence.latestFailedRefreshJob;
    failedEl.innerHTML = failed
      ? '<div class="fo-kv"><span>' + escapeHtml(foundationLabels.run) + '</span><b>#' + failed.id + '</b></div><div class="fo-job-error">' + escapeHtml(failed.error_summary || foundationLabels.unknownFailure) + '</div>'
      : '<div class="fo-empty">' + escapeHtml(labels.latestJobsNoFailures) + '</div>';
    const jobs = data.refreshJobs || [];
    jobsEl.innerHTML = jobs.length
      ? '<div class="aa-table-shell"><table class="data-table fo-table"><thead><tr><th>' + escapeHtml(foundationLabels.run) + '</th><th>' + escapeHtml(foundationLabels.scope) + '</th><th>' + escapeHtml(foundationLabels.period) + '</th><th>' + escapeHtml(foundationLabels.statusHeader) + '</th><th>' + escapeHtml(foundationLabels.started) + '</th><th>' + escapeHtml(foundationLabels.completedAt) + '</th></tr></thead><tbody>' + jobs.map(foFormatJob).join('') + '</tbody></table></div>'
      : '<div class="fo-empty">' + escapeHtml(labels.noFoundationJobs) + '</div>';
    if (subtitle) subtitle.textContent = labels.recentRead + new Date().toLocaleTimeString();
  } catch (e) {
    completenessEl.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.readFailed + e.message) + '</div>';
    cadenceEl.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.readFailed + e.message) + '</div>';
    jobsEl.innerHTML = '<div class="fo-job-error">' + escapeHtml(labels.readFailed + e.message) + '</div>';
  }
}

async function foundationRefreshRange(startDate, days, buttonId) {
  const labels = aiAssetsText();
  const btn = document.getElementById(buttonId);
  const original = btn ? btn.textContent : labels.refresh;
  if (btn) {
    btn.disabled = true;
    btn.textContent = labels.submitting;
  }
  try {
    const res = await fetch('/api/weekly-report/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start: startDate, days})
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const queued = await res.json();
    await waitForFoundationRefresh(queued.runId, status => {
      if (btn) btn.textContent = status === 'queued' ? labels.queued : labels.updating;
    });
    await loadFoundationOps();
  } catch (e) {
    window.alert(labels.refreshFailed + e.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

function foundationRefreshToday() {
  foundationRefreshRange(foDateString(foToday()), 1, 'foRefreshTodayBtn');
}

function foundationRefreshCurrentWeek() {
  const start = foCurrentWeekStart();
  foundationRefreshRange(foDateString(start), foDaysBetween(start, foToday()), 'foRefreshWeekBtn');
}

function foundationRefreshCurrentMonth() {
  const start = foCurrentMonthStart();
  foundationRefreshRange(foDateString(start), foDaysBetween(start, foToday()), 'foRefreshMonthBtn');
}

async function foundationBackfillRange() {
  const labels = aiAssetsText();
  const start = document.getElementById('foBackfillStart')?.value;
  const end = document.getElementById('foBackfillEnd')?.value;
  const status = document.getElementById('foBackfillStatus');
  const btn = document.getElementById('foBackfillBtn');
  if (!start || !end) {
    if (status) status.textContent = operatorText().dateRangeRequired;
    return;
  }
  if (btn) btn.disabled = true;
  if (status) status.textContent = labels.submitting;
  try {
    const res = await fetch('/api/foundation/backfill', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start, end})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (status) status.textContent = labels.submittedRun + data.runId + ' · ' + data.start + foundationText().dateRangeTo + data.end;
    await waitForFoundationRefresh(data.runId, jobStatus => {
      if (status) status.textContent = 'Run #' + data.runId + ' · ' + foStatusLabel(jobStatus);
    });
    await loadFoundationOps();
  } catch (e) {
    if (status) status.textContent = labels.backfillFailed + e.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function aaEnsureAssetsLoaded() {
  const page = document.getElementById('page-static');
  if (!page || !page.classList.contains('active')) return;
  const content = document.getElementById('aiAssetsContent');
  const loading = document.getElementById('aiAssetsLoading');
  const hasData = content && content.style.display === 'block' && _aaState.data;
  const failed = loading && loading.innerHTML.startsWith('❌');
  if (!hasData && !failed) loadAiAssets();
}


function aaRender(d) {
  const labels = aiAssetsText();
  _aaState.data = d;
  const el = id => document.getElementById(id);
  const tools = d.tools || [];
  const diary = d.diary || {};
  const rag = d.rag || {};
  const cronJobs = d.cronJobs || {};

  // A: KPIs
  el('aaKpi').innerHTML = [
    { icon:'🔥', label:labels.totalTokens, value: aaFmtTokens(d.totalTokens||0) },
    { icon:'💬', label:labels.totalMessages, value: (d.totalMessages||0).toLocaleString() },
    { icon:'⚡', label:labels.activeSystems, value: tools.length + (labels.countUnit ? ' ' + labels.countUnit : '') },
    { icon:'🤖', label:labels.agentInstances, value: (d.agents||[]).length },
    { icon:'📅', label:labels.lookbackDays, value: '67 ' + labels.dayUnit },
  ].map(k => '<div class="aa-kpi-card"><div class="aa-kpi-icon">' + k.icon + '</div><div class="aa-kpi-label">' + k.label + '</div><div class="aa-kpi-value">' + k.value + '</div></div>').join('');

  // B: Tools
  const maxTokens = Math.max(...tools.map(t => t.allTimeTokens), 1);
  el('aaTools').innerHTML = tools.map(t => {
    const pct = (t.allTimeTokens / maxTokens * 100).toFixed(1);
    const usageUnavailable = t.usageStatus === 'unavailable';
    const usagePartial = t.usageStatus === 'local-partial';
    const allTimeUsage = usageUnavailable ? labels.localUsageUnavailable : aaFmtTokens(t.allTimeTokens);
    const todayUsage = usageUnavailable ? labels.localUsageUnavailable : aaFmtTokens(t.todayTokens);
    return '<div class="aa-tool-card">' +
        '<div class="aa-tool-header"><span class="aa-tool-name">' + t.name + '</span>' +
        (usagePartial ? '<span class="aa-tool-usage-note">' + escapeHtml(labels.localUsagePartial) + '</span>' : '') +
        '<span class="aa-tool-emoji">' + (t.emoji||'') + '</span></div>' +
        '<div class="aa-tool-stat"><span>' + escapeHtml(labels.cumulativeUsage) + '</span><span class="aa-tool-stat-val">' + escapeHtml(allTimeUsage) + '</span></div>' +
        '<div class="aa-tool-stat"><span>' + escapeHtml(labels.todayUsage) + '</span><span class="aa-tool-stat-val" style="color:var(--purple)">' + escapeHtml(todayUsage) + '</span></div>' +
        '<div class="aa-tool-bar-track"><div class="aa-tool-bar-fill" style="width:' + pct + '%; background:' + aaToolColor(t.name) + '"></div></div>' +
        '<div class="aa-tool-dates"><span>' + escapeHtml(labels.firstActive) + (t.firstActivity||'—') + '</span><span>' + escapeHtml(labels.lastActive) + (t.lastActivity||'—') + '</span></div>' +
      '</div>';
  }).join('');

  // C & D: Charts (heatmap + dual bar)
  if (typeof renderAACharts === 'function') renderAACharts(d);

  // E: Agent table
  const agentTbody = document.querySelector('#aaAgentTable tbody');
  if (agentTbody) {
    agentTbody.innerHTML = (d.agents || []).map((a, idx) =>
      '<tr data-aa-agent-row="' + idx + '" style="cursor:pointer"><td>' + escapeHtml(a.displayName || a.name) + '</td><td>' + escapeHtml(a.model || '—') + '</td><td>' + (a.sessionCount||0) + '</td><td>' + (a.totalMessages||0).toLocaleString() + '</td><td>' + escapeHtml(a.lastActive || '—') + '</td><td>' + escapeHtml(a.source || '') + '</td></tr>'
    ).join('');
  }

  // F: 资产积累
  if(el('aaDiary')) el('aaDiary').innerHTML = [['📝 ' + labels.diaryCount, (diary.count||0).toLocaleString()], ['📅 ' + labels.firstDiary, diary.firstDate||'—'], ['📅 ' + labels.lastDiary, diary.lastDate||'—'], ['📝 ' + labels.totalWords, (diary.totalWords||0).toLocaleString()]].map(r => '<div class="aa-info-row"><span>'+escapeHtml(r[0])+'</span><span>'+r[1]+'</span></div>').join('');

  // G: 服务状态
  if(el('aaSkills')) el('aaSkills').innerHTML = Object.entries(d.skills?.byTool || {}).map(([tool, skills]) =>
    '<div class="aa-info-row"><span>' + tool + '</span><span>' + skills.length + ' Skills</span></div>'
  ).join('');
  if(el('aaRag')) el('aaRag').innerHTML = renderAaRagStatus(rag, labels);
  if(el('aaCron')) el('aaCron').innerHTML = [[labels.total, cronJobs.total||0], [labels.success, cronJobs.success||0], [labels.failed, cronJobs.failed||0], [labels.successRate, (cronJobs.successRate||0) + '%']].map(r => '<div class="aa-info-row"><span>'+escapeHtml(r[0])+'</span><span>'+r[1]+'</span></div>').join('');

  // H: 基础设施
  if(el('aaDevices')) el('aaDevices').innerHTML = (d.infrastructure?.devices || []).length
    ? (d.infrastructure.devices).map((dev, index) =>
      '<details class="aa-device-card"' + (index === 0 ? ' open' : '') + '>' +
        '<summary class="aa-device-summary"><span class="aa-device-identity"><span class="aa-device-emoji">' + escapeHtml(dev.emoji || '') + '</span><span><span class="aa-device-name">' + escapeHtml(dev.name) + '</span><span class="aa-device-role">' + escapeHtml(dev.role || '') + '</span></span></span>' +
        '<span class="aa-device-count">' + (dev.services || []).length + ' ' + escapeHtml(labels.servicesUnit) + '</span><span class="aa-device-status">' + escapeHtml(dev.status || '') + '</span></summary>' +
        '<div class="aa-device-service-list">' + (dev.services || []).map(service =>
          '<div class="aa-device-service-row"><span class="aa-service-main"><strong>' + escapeHtml(service.name) + '</strong><small>' + escapeHtml(service.type || '') + '</small></span>' +
          '<span class="aa-service-port">' + escapeHtml(service.port || '—') + '</span><span class="aa-service-state">' + escapeHtml(service.status || '') + '</span></div>'
        ).join('') + '</div>' +
      '</details>'
    ).join('')
    : '<div style="color:var(--slate);font-size:13px">' + escapeHtml(labels.noDeviceData) + '</div>';

  // I: 存储使用
  renderStorageDetail(d.storage || {});

  // K: Agent 配置面板
  renderAgentPanel(d.agentTree || []);

  // L: Skill 库
  renderSkills(d.skills || {});

  // M: 工具配置
  if (typeof renderToolConfigs === 'function') renderToolConfigs(d.toolConfigs || []);
}

function switchSkillTab(type) {
  _aaState.skillTab = type;
  _aaSkillTab = type;
  renderSkills(window._aaSkillsData || _aaState.data?.skills || {});
}

let _editingPath = null;
async function openEditor(agentName, fileName, path) {
  const labels = aiAssetsText();
  _editingPath = path;
  const title = document.getElementById('aaEditorTitle');
  if(title) title.innerHTML = '<span style="color:#533afd">' + agentName + '</span> / ' + fileName;
  const ta = document.getElementById('aaEditorTextarea');
  if(ta) ta.value = labels.readingFile;
  const overlay = document.getElementById('aaEditorOverlay');
  if(overlay) overlay.style.display = 'flex';
  try {
    const res = await fetch('/api/file-content?path=' + encodeURIComponent(path));
    const d = await res.json();
    if(ta) ta.value = d.content || d.error;
  } catch (e) { if(ta) ta.value = labels.readFailed + e.message; }
}
function closeEditor() {
  const overlay = document.getElementById('aaEditorOverlay');
  if(overlay) overlay.style.display = 'none';
}
async function saveFile() {
  const labels = aiAssetsText();
  const ta = document.getElementById('aaEditorTextarea');
  if(!ta) return;
  const content = ta.value;
  const confirmationText = 'SAVE ACTANARA FILE';
  const typed = prompt(labels.savePrompt + confirmationText);
  if (typed !== confirmationText) {
    alert(labels.saveCancelled);
    return;
  }
  try {
    const res = await fetch('/api/file-content', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: _editingPath, content, confirmationText })
    });
    const d = await res.json();
    if (d.success) { alert(labels.saveSuccess + (d.backupPath ? '\nBackup: ' + d.backupPath : '')); closeEditor(); }
    else { alert(labels.saveFailed + (d.error || d.message || 'unknown')); }
  } catch(e) { alert(labels.systemError + e.message); }
}

// ═══════════════════════════════════════════════════════
// Storage Detail Renderer
// ═══════════════════════════════════════════════════════
function renderStorageDetail(storage) {
  const labels = aiAssetsText();
  const container = document.getElementById('aaStorage');
  if (!container) return;
  let html = '';

  // Per-tool disk usage
  const toolStorage = storage.tools || [];
  if (toolStorage.length) {
    html += '<div class="aa-storage-section"><div class="aa-storage-section-title">' + escapeHtml(labels.toolStorage) + '</div>';
    html += '<div class="aa-storage-tool-grid">';
    html += toolStorage.map(t =>
      '<div class="aa-storage-tool-card"><div class="aa-storage-tool-emoji">' + t.emoji + '</div>' +
      '<div class="aa-storage-tool-name">' + t.name + '</div>' +
      '<div class="aa-storage-tool-size">' + t.sizeMB + '</div>' +
      '<div class="aa-storage-unit">MB</div></div>'
    ).join('');
    html += '</div></div>';
  }

  // Category breakdown
  const categories = storage.categories || [];
  if (categories.length) {
    html += '<div class="aa-storage-section"><div class="aa-storage-section-title">' + escapeHtml(labels.artifactDetails) + '</div>';
    const maxCat = Math.max(...categories.map(c => c.sizeMB), 1);
    html += categories.map(c => {
      const pct = (c.sizeMB / maxCat * 100).toFixed(0);
      return '<div class="aa-storage-row"><span class="aa-storage-label">' + c.label + '</span>' +
        '<div class="aa-storage-bar-track"><div class="aa-storage-bar-fill" style="width:' + pct + '%;background:var(--purple)"></div></div>' +
        '<span class="aa-storage-text">' + c.sizeMB + ' MB</span></div>';
    }).join('');
    html += '</div>';
  }

  container.innerHTML = html || '<div style="color:var(--slate);font-size:13px">' + escapeHtml(labels.noStorageData) + '</div>';
}

// ═══════════════════════════════════════════════════════
// Agent Panel — Modal-based
// ═══════════════════════════════════════════════════════
function renderAgentPanel(agentTree) {
  const labels = aiAssetsText();
  const container = document.getElementById('aaAgentPanel');
  if (!container) return;
  if (!agentTree.length) { container.innerHTML = '<div style="color:var(--slate);font-size:13px">' + escapeHtml(labels.noAgentData) + '</div>'; return; }

  container.innerHTML = '<div class="aa-agent-panel-tools">' +
    agentTree.map((tool, ti) =>
      '<div class="aa-ap-tool-card" data-aa-agent-tool="' + ti + '">' +
        '<div class="aa-ap-tool-emoji">' + tool.emoji + '</div>' +
        '<div class="aa-ap-tool-name">' + tool.name + '</div>' +
        '<div class="aa-ap-tool-count">' + tool.count + '</div>' +
        '<div class="aa-ap-tool-label">' + tool.countLabel + '</div>' +
      '</div>'
    ).join('') +
  '</div>';
}

let _aaModalBackAction = null;

function _showModalContent(html, title, options) {
  options = options || {};
  const modal = document.getElementById('aaDocModal');
  if (!modal) { console.error('aaDocModal not found'); return; }
  if (modal.parentElement !== document.body) document.body.appendChild(modal);
  const panel = modal.querySelector('.aa-modal');
  const titleEl = document.getElementById('aaDocModalTitle');
  const subtitleEl = document.getElementById('aaDocModalSubtitle');
  const backBtn = document.getElementById('aaModalBack');
  const textarea = document.getElementById('aaDocTextarea');
  const content = document.getElementById('aaModalContent');

  // Reset to view mode (not editing)
  modal.classList.remove('aa-editing');
  if (panel) { panel.style.removeProperty('width'); panel.style.removeProperty('max-height'); }
  const body = modal.querySelector('.aa-modal-body');
  if (body) { body.style.removeProperty('display'); body.style.removeProperty('flex-direction'); }

  _styleAaModalPanel(modal, panel, content);
  if (titleEl) titleEl.innerHTML = title;
  if (subtitleEl) subtitleEl.textContent = '';
  _aaModalBackAction = typeof options.onBack === 'function' ? options.onBack : null;
  if (backBtn) backBtn.style.display = _aaModalBackAction ? 'inline-flex' : 'none';
  if (textarea) { textarea.style.display = 'none'; textarea.style.removeProperty('flex'); }
  if (content) { content.innerHTML = html; content.style.display = 'block'; }
  const footer = modal.querySelector('.aa-modal-footer');
  if (footer) footer.style.display = 'none';
  modal.classList.add('active');
  modal.style.setProperty('display', 'flex', 'important');
  modal.style.setProperty('position', 'fixed', 'important');
  modal.style.setProperty('inset', '0', 'important');
  modal.style.setProperty('z-index', '999999', 'important');
  modal.style.setProperty('align-items', 'center', 'important');
  modal.style.setProperty('justify-content', 'center', 'important');
  modal.style.setProperty('background', 'rgba(6, 27, 49, 0.62)', 'important');
}

function aaModalBack() {
  if (typeof _aaModalBackAction === 'function') _aaModalBackAction();
}

function _styleAaModalPanel(modal, panel, content) {
  if (modal) {
    modal.style.setProperty('display', 'flex', 'important');
    modal.style.setProperty('position', 'fixed', 'important');
    modal.style.setProperty('inset', '0', 'important');
    modal.style.setProperty('width', '100vw', 'important');
    modal.style.setProperty('height', '100vh', 'important');
    modal.style.setProperty('z-index', '999999', 'important');
    modal.style.setProperty('align-items', 'center', 'important');
    modal.style.setProperty('justify-content', 'center', 'important');
    modal.style.setProperty('padding', '24px', 'important');
    modal.style.setProperty('background', 'rgba(6, 27, 49, 0.62)', 'important');
  }
  if (panel) {
    panel.style.setProperty('background', '#ffffff', 'important');
    panel.style.setProperty('color', '#061b31', 'important');
    panel.style.setProperty('border', '1px solid rgba(142, 154, 175, 0.28)', 'important');
    panel.style.setProperty('border-radius', '18px', 'important');
    panel.style.setProperty('width', 'min(920px, calc(100vw - 48px))', 'important');
    panel.style.setProperty('max-width', '920px', 'important');
    panel.style.setProperty('max-height', '86vh', 'important');
    panel.style.setProperty('min-height', '220px', 'important');
    panel.style.setProperty('display', 'flex', 'important');
    panel.style.setProperty('flex-direction', 'column', 'important');
    panel.style.setProperty('box-shadow', '0 30px 90px rgba(6, 27, 49, 0.28)', 'important');
    panel.style.setProperty('overflow', 'hidden', 'important');
  }
  if (content) {
    content.style.setProperty('background', '#fff', 'important');
    content.style.setProperty('color', '#061b31', 'important');
    content.style.setProperty('overflow-y', 'auto', 'important');
    content.style.setProperty('max-height', 'calc(86vh - 78px)', 'important');
    content.style.setProperty('white-space', 'normal', 'important');
    content.style.setProperty('overflow-wrap', 'anywhere', 'important');
    content.style.setProperty('word-break', 'break-word', 'important');
  }
}

function _aaLevelStrip(level, count) {
  const labels = aiAssetsText();
  const levelLabels = labels.levelLabels || {};
  const defs = {
    global:    { cls: 'aa-modal-strip-global',    icon: '🌐', label: levelLabels.global || 'Global' },
    workspace: { cls: 'aa-modal-strip-workspace',  icon: '📂', label: levelLabels.workspace || 'Workspace' },
    agent:     { cls: 'aa-modal-strip-agent',      icon: '🦞', label: levelLabels.agent || 'Agent' },
    session:   { cls: 'aa-modal-strip-session',    icon: '💬', label: levelLabels.session || 'Session' },
  };
  const d = defs[level] || defs.session;
  return '<div class="aa-modal-strip ' + d.cls + '">' +
    '<span class="aa-modal-strip-icon">' + d.icon + '</span>' +
    '<span class="aa-modal-strip-label">' + d.label + '</span>' +
    (count > 0 ? '<span class="aa-modal-strip-count">' + count + ' ' + escapeHtml(labels.itemUnit) + '</span>' : '') +
  '</div>';
}

function _aaFileRow(f, itemName) {
  const labels = aiAssetsText();
  const exists = f.exists !== false;
  const sizeStr = exists ? (f.size > 1024 ? (f.size / 1024).toFixed(1) + ' KB' : f.size + ' B') : labels.missingFile;
  const kind = f.kind || (f.isSkill ? 'skill' : 'context');
  const kindLabels = labels.fileKindLabels || {};
	  const kindMeta = {
	    context: { icon: '📄', label: kindLabels.context || 'Context' },
	    config: { icon: '⚙️', label: kindLabels.config || 'Config' },
	    command: { icon: '⌘', label: kindLabels.command || 'Command' },
	    reference: { icon: '📚', label: kindLabels.reference || 'Reference' },
	    skill: { icon: '⚡', label: kindLabels.skill || 'Skill' },
	    memory: { icon: '🧠', label: kindLabels.memory || 'Memory' },
	  };
  const meta = kindMeta[kind] || kindMeta.context;
  return '<div class="aa-file-row' + (exists ? '' : ' aa-file-row-missing') + '" data-aa-doc-path="' + escapeHtml(f.path) + '" data-aa-doc-agent="' + escapeHtml(itemName) + '" data-aa-doc-name="' + escapeHtml(f.name) + '" data-aa-doc-create="' + (!exists && f.createable ? '1' : '0') + '">' +
    '<span class="aa-file-icon">' + meta.icon + '</span>' +
    '<span class="aa-file-main"><span class="aa-file-name">' + escapeHtml(f.name) + '</span><span class="aa-file-path">' + escapeHtml(f.path || '') + '</span></span>' +
    '<span class="aa-file-kind">' + meta.label + '</span>' +
    '<span class="aa-file-size' + (exists ? '' : ' aa-file-size-missing') + '">' + sizeStr + '</span>' +
	  '</div>';
	}

function _aaGroupedFileRows(files, itemName) {
  const labels = aiAssetsText();
  const descs = labels.fileGroupDescriptions || {};
  const titles = labels.fileGroupTitles || {};
  const groups = [
    { key: 'context', title: titles.context || 'Context Instructions', desc: descs.context || '', open: true },
    { key: 'config', title: titles.config || 'Runtime Config', desc: descs.config || '', open: false },
    { key: 'tools', title: titles.tools || 'Commands / Plugin Assets', desc: descs.tools || '', open: false },
  ];
  const buckets = { context: [], config: [], tools: [] };
  (files || []).forEach(f => {
    const key = f.group || ((f.kind === 'config') ? 'config' : (f.kind === 'command' || f.kind === 'reference' ? 'tools' : 'context'));
    (buckets[key] || buckets.context).push(f);
  });
  return groups.filter(g => buckets[g.key].length).map(g => {
    const rows = buckets[g.key].map(f => _aaFileRow(f, itemName)).join('');
    return '<details class="aa-file-group aa-file-group-' + g.key + '"' + (g.open ? ' open' : '') + '>' +
      '<summary class="aa-file-group-summary">' +
        '<span><b>' + g.title + '</b><small>' + g.desc + '</small></span>' +
        '<span class="aa-file-group-count">' + buckets[g.key].length + '</span>' +
      '</summary>' +
      '<div class="aa-file-group-body">' + rows + '</div>' +
    '</details>';
  }).join('');
}

function _aaWorkspaceBucket(item) {
  const labels = aiAssetsText();
  const bucketLabels = labels.workspaceBuckets || {};
  const key = item.workspaceGroup || 'project';
	    const defs = {
	      current: { ...(bucketLabels.current || { title: 'Current Project', desc: '' }), open: true },
	      project: { ...(bucketLabels.project || { title: 'Project Workspaces', desc: '' }), open: true },
	      home: { ...(bucketLabels.home || { title: 'CLI Home / General', desc: '' }), open: false },
	      general: { ...(bucketLabels.general || { title: 'General / Broad Directory', desc: '' }), open: false },
	      external: { ...(bucketLabels.external || { title: 'External / Probe', desc: '' }), open: false },
	    };
  return { key, ...(defs[key] || defs.project) };
}

function _aaRenderWorkspaceItem(toolIdx, item, ii) {
  const labels = aiAssetsText();
  const hasFiles = (item.keyFiles || []).length > 0;
  const badge = item.workspaceGroup ? '<span class="aa-workspace-badge aa-workspace-badge-' + escapeHtml(item.workspaceGroup) + '">' + escapeHtml(item.workspaceGroup) + '</span>' : '';
  return '<div class="aa-ap-item" style="cursor:pointer" data-aa-agent-tool="' + toolIdx + '" data-aa-agent-item="' + ii + '">' +
    '<div class="aa-ap-item-header">' +
      '<span class="aa-ap-item-name">' + escapeHtml(item.displayName || item.name) + '</span>' +
      badge +
      (item.model ? '<span class="aa-ap-item-meta">' + escapeHtml(item.model) + '</span>' : '') +
    '</div>' +
    '<div class="aa-ap-item-detail">' +
      '<span>' + escapeHtml(labels.sessions) + ': <span class="val">' + item.sessions + '</span></span>' +
      '<span>' + escapeHtml(labels.messages) + ': <span class="val">' + (item.messages||0).toLocaleString() + '</span></span>' +
      '<span>' + escapeHtml(labels.lastActive) + ': <span class="val">' + escapeHtml(item.lastActive || '—') + '</span></span>' +
    '</div>' +
    (item.workspace ? '<div class="aa-workspace-path">' + escapeHtml(item.workspace) + '</div>' : '') +
    (hasFiles ? '<div style="margin-top:6px;font-size:11px;color:var(--purple)">📄 ' + escapeHtml(labels.keyFilesHint(item.keyFiles.length)) + '</div>' : '') +
  '</div>';
}

function openApToolModal(toolIdx) {
  const agentTree = _aaState.data?.agentTree || [];
  const tool = agentTree[toolIdx];
  if (!tool) return;

  const items = tool.items || [];

  // Group items by level
  const levels = {};
  items.forEach((item, ii) => {
    const lvl = item.level || 'session';
    if (!levels[lvl]) levels[lvl] = [];
    levels[lvl].push({ item, ii });
  });

  // Render order: global → workspace → agent → session
  const order = ['global', 'workspace', 'agent', 'session'];
  let html = '<div style="max-height:60vh;overflow-y:auto">';

  for (const lvl of order) {
    if (!levels[lvl] || !levels[lvl].length) continue;
    const group = levels[lvl];

    html += '<div class="aa-modal-section">';
    html += _aaLevelStrip(lvl, group.length);
    html += '<div class="aa-modal-section-items">';

	    if (lvl === 'workspace' && (tool.name === 'Codex' || tool.name === 'Claude Code')) {
	      const buckets = {};
	      group.forEach(({ item, ii }) => {
	        const b = _aaWorkspaceBucket(item);
	        if (!buckets[b.key]) buckets[b.key] = { ...b, rows: [] };
	        buckets[b.key].rows.push({ item, ii });
	      });
	      ['current', 'project', 'general', 'home', 'external'].forEach(key => {
	        const b = buckets[key];
	        if (!b || !b.rows.length) return;
	        html += '<details class="aa-workspace-group aa-workspace-group-' + key + '"' + (b.open ? ' open' : '') + '>' +
	          '<summary class="aa-workspace-summary"><span><b>' + b.title + '</b><small>' + b.desc + '</small></span><span class="aa-file-group-count">' + b.rows.length + '</span></summary>' +
	          '<div class="aa-workspace-group-body">';
	        b.rows.forEach(({ item, ii }) => { html += _aaRenderWorkspaceItem(toolIdx, item, ii); });
	        html += '</div></details>';
	      });
	      html += '</div></div>';
	      continue;
	    }

	    for (const { item, ii } of group) {
	      const hasFiles = (item.keyFiles || []).length > 0;
	      const isGlobal = item.level === 'global';

      if (isGlobal) {
        // Global items show files directly
	        const files = item.keyFiles || [];
	        if (files.length) {
	          html += '<div style="padding:8px 0">';
	          html += _aaGroupedFileRows(files, item.name);
	          html += '</div>';
	        }
	      } else {
	        html += _aaRenderWorkspaceItem(toolIdx, item, ii);
	      }
    }
    html += '</div></div>';
  }
  html += '</div>';
  _showModalContent(html, tool.emoji + ' ' + tool.name + ' <span style="color:var(--slate);font-weight:400;font-size:13px">(' + tool.count + ' ' + tool.countLabel + ')</span>');
}

function openApItemModal(toolIdx, itemIdx) {
  const labels = aiAssetsText();
  const agentTree = _aaState.data?.agentTree || [];
  const tool = agentTree[toolIdx];
  const item = (tool?.items || [])[itemIdx];
  if (!item) return;

  const files = item.keyFiles || [];
  const levelLabel = { global: '🌐 Global', workspace: '📂 Workspace', agent: '🦞 Agent', session: '💬 Session' };
  let html = '<div style="padding:4px 0">';
  html += '<div style="margin-bottom:16px">';
  html += '<div style="font-size:14px;font-weight:700;color:var(--navy);margin-bottom:8px">' + escapeHtml(item.displayName || item.name) + '</div>';
  html += '<div style="display:flex;gap:16px;font-size:12px;color:var(--slate);flex-wrap:wrap">';
  html += '<span>' + (levelLabel[item.level] || '') + '</span>';
  if (item.model) html += '<span>' + escapeHtml(labels.modelLabel) + ': <b style="color:var(--navy)">' + escapeHtml(item.model) + '</b></span>';
  html += '<span>' + escapeHtml(labels.sessions) + ': <b style="color:var(--navy)">' + item.sessions + '</b></span>';
  html += '<span>' + escapeHtml(labels.messages) + ': <b style="color:var(--navy)">' + (item.messages||0).toLocaleString() + '</b></span>';
  html += '<span>' + escapeHtml(labels.lastActive) + ': <b style="color:var(--navy)">' + escapeHtml(item.lastActive || '—') + '</b></span>';
  html += '</div>';
  if (item.workspace) {
    html += '<div style="font-size:11px;color:var(--slate);font-family:var(--font-mono);word-break:break-all;margin-top:8px">' + escapeHtml(item.workspace) + '</div>';
  }
  html += '</div>';

	  if (files.length) {
	    html += _aaLevelStrip(item.level || 'session', files.length);
	    html += '<div style="padding:4px 0">';
	    html += _aaGroupedFileRows(files, item.name);
	    html += '</div>';
  } else {
    html += '<div style="color:var(--slate);font-size:13px">' + escapeHtml(labels.noKeyFiles) + '</div>';
  }
  html += '</div>';
  _showModalContent(html, tool.emoji + ' ' + (item.displayName || item.name), {
    onBack: () => openApToolModal(toolIdx)
  });
}

function openAaAgentRowModal(idx) {
  const labels = aiAssetsText();
  const agent = (_aaState.data?.agents || [])[idx];
  if (!agent) return;

  const docs = Object.entries(agent.documents || {}).filter(([, info]) => info && info.exists);
  let html = '<div style="padding:4px 0">';
  html += '<div style="font-size:14px;font-weight:700;color:var(--navy);margin-bottom:8px">' + escapeHtml(agent.displayName || agent.name || agent.id) + '</div>';
  html += '<div style="display:flex;gap:16px;font-size:12px;color:var(--slate);flex-wrap:wrap;margin-bottom:16px">';
  html += '<span>' + escapeHtml(labels.modelLabel) + ': <b style="color:var(--navy)">' + escapeHtml(agent.model || '—') + '</b></span>';
  html += '<span>' + escapeHtml(labels.sessions) + ': <b style="color:var(--navy)">' + (agent.sessionCount || 0) + '</b></span>';
  html += '<span>' + escapeHtml(labels.messages) + ': <b style="color:var(--navy)">' + (agent.totalMessages || 0).toLocaleString() + '</b></span>';
  html += '<span>' + escapeHtml(labels.sourceLabel) + ': <b style="color:var(--navy)">' + escapeHtml(agent.source || '—') + '</b></span>';
  html += '<span>' + escapeHtml(labels.lastActive) + ': <b style="color:var(--navy)">' + escapeHtml(agent.lastActive || '—') + '</b></span>';
  html += '</div>';
  if (agent.workspace) {
    html += '<div style="font-size:11px;color:var(--slate);font-family:var(--font-mono);word-break:break-all;margin-bottom:14px">' + escapeHtml(agent.workspace) + '</div>';
  }

  if (docs.length) {
    html += _aaLevelStrip('global', docs.length);
    html += '<div style="padding:4px 0">';
    html += docs.map(([name, info]) => {
      const path = info.path || (agent.workspace ? agent.workspace.replace(/\/$/, '') + '/' + name : name);
      const lines = info.lines ? info.lines + ' ' + labels.linesUnit : '';
      return '<div style="display:flex;align-items:center;gap:8px;padding:10px 12px;background:#f8f9fc;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;cursor:pointer" data-aa-doc-path="' + escapeHtml(path) + '" data-aa-doc-agent="' + escapeHtml(agent.name || agent.id || 'Agent') + '" data-aa-doc-name="' + escapeHtml(name) + '">' +
        '<span style="font-size:16px">📄</span>' +
        '<span style="font-family:var(--font-mono);font-size:13px;color:var(--navy);flex:1">' + escapeHtml(name) + '</span>' +
        '<span style="font-size:11px;color:var(--slate);font-family:var(--font-mono)">' + escapeHtml(lines) + '</span>' +
      '</div>';
    }).join('');
    html += '</div>';
  } else {
    html += '<div style="color:var(--slate);font-size:13px">' + escapeHtml(labels.noKeyFiles) + '</div>';
  }
  html += '</div>';

  _showModalContent(html, '🤖 ' + (agent.displayName || agent.name || agent.id));
}




// ═══════════════════════════════════════════════════════
// Section J: Skill Library
// ═══════════════════════════════════════════════════════
let _aaSkillTab = 'global';
let _aaSkillSearch = '';
let _aaSkillCollapsed = {};
let _aaSkillSearchCursor = null;

let _aaExpandedSkill = null;

function aaSkillLevelRank(level) {
  const order = {
    global: 10,
    workspace: 20,
    project: 30,
    agent: 40,
    system: 50
  };
  return order[level || ''] || 999;
}

function aaToggleSkillGroup(tab, level) {
  const key = tab + '::' + level;
  _aaSkillCollapsed[key] = !_aaSkillCollapsed[key];
  renderSkills();
}

function aaSkillMatches(s, q) {
  if (!q) return true;
  return (s.name || '').toLowerCase().includes(q) ||
    (s.description || '').toLowerCase().includes(q) ||
    (s.searchText || '').toLowerCase().includes(q) ||
    (s.category || '').toLowerCase().includes(q) ||
    (s.profile || '').toLowerCase().includes(q) ||
    (s.sourceKindLabel || s.sourceKind || '').toLowerCase().includes(q);
}

function aaSkillSearchInput(input) {
  _aaSkillSearch = input.value;
  _aaSkillSearchCursor = input.selectionStart;
  renderSkills();
  requestAnimationFrame(() => {
    const next = document.getElementById('aaSkillSearchInput');
    if (!next) return;
    next.focus();
    const pos = Math.min(_aaSkillSearchCursor ?? next.value.length, next.value.length);
    try { next.setSelectionRange(pos, pos); } catch (e) {}
  });
}

function aaRenderSkillLevelSections(tab, list, filtered) {
  const groups = {};
  filtered.forEach(s => {
    const level = s.level || s.type || 'global';
    const groupKey = level === 'agent' && s.profile ? level + ':' + s.profile : level;
    const label = level === 'agent' && s.profile ? 'Agent · ' + s.profile : (s.levelLabel || level);
    if (!groups[groupKey]) groups[groupKey] = {level, label, items: []};
    groups[groupKey].items.push(s);
  });

  return Object.keys(groups)
    .sort((a, b) => aaSkillLevelRank(groups[a].level) - aaSkillLevelRank(groups[b].level) || a.localeCompare(b))
    .map(groupKey => {
      const group = groups[groupKey];
      const key = tab + '::' + groupKey;
      const collapsed = !!_aaSkillCollapsed[key];
      const items = group.items.map(s => {
        const category = s.category ? '<span class="aa-skill-capsule-source">' + escapeHtml(s.category) + '</span>' : '';
        const profile = s.profile ? '<span class="aa-skill-capsule-source">' + escapeHtml(s.profile) + '</span>' : '';
        return '<span class="aa-skill-capsule" data-tab="' + escapeHtml(tab) + '" data-idx="' + list.indexOf(s) + '">' +
          '<span class="aa-skill-capsule-name">' + escapeHtml(s.name) + '</span>' + profile + category +
        '</span>';
      }).join('');
      return '<section class="aa-skill-level-group ' + (collapsed ? 'collapsed' : '') + '">' +
        '<button type="button" class="aa-skill-level-head" onclick="aaToggleSkillGroup(\'' + escapeHtml(tab) + '\',\'' + escapeHtml(groupKey) + '\')">' +
          '<span class="aa-skill-level-title"><span class="aa-skill-level-chevron">' + (collapsed ? '›' : '⌄') + '</span>' + escapeHtml(group.label) + '</span>' +
          '<span>' + group.items.length + '</span>' +
        '</button>' +
        '<div class="aa-skill-capsules aa-skill-capsules-nested" style="' + (collapsed ? 'display:none' : '') + '">' + items + '</div>' +
      '</section>';
    }).join('');
}

function renderSkills(skills) {
  const labels = aiAssetsText();
  skills = skills || window._aaSkillsData || _aaState.data?.skills || {};
  const container = document.getElementById('aaSkillLib');
  if (!container) return;

  const byTool = skills.byTool || {};
  const toolNames = Object.keys(byTool);
  if (!toolNames.length) { container.innerHTML = '<div style="color:var(--slate);font-size:13px">' + escapeHtml(labels.noSkillsData) + '</div>'; return; }

  if (!_aaSkillTab || !byTool[_aaSkillTab]) _aaSkillTab = toolNames[0];

  const tabs = toolNames.map(t =>
    `<button class="aa-skill-tab ${_aaSkillTab===t?'active':''}" onclick="_aaSkillTab='${t}';renderSkills()">` +
      `<span class="aa-skill-tab-icon">${aaToolEmoji(t)}</span>` +
      `<span class="aa-skill-tab-main"><span class="aa-skill-tab-name">${escapeHtml(t)}</span><span class="aa-skill-tab-meta">${escapeHtml(labels.skillsLibrary)}</span></span>` +
      `<span class="aa-skill-count">${byTool[t].length}</span>` +
    `</button>`
  ).join('');

  container.innerHTML = `
    <input id="aaSkillSearchInput" class="aa-skill-search" placeholder="${escapeHtml(labels.skillSearchPlaceholder)}" value="${escapeHtml(_aaSkillSearch)}" oninput="aaSkillSearchInput(this)">
    <div class="aa-skill-tabs">${tabs}</div>
    <div class="aa-skill-levels" id="aaSkillGrid"></div>
  `;

  const q = _aaSkillSearch.toLowerCase();
  const grid = document.getElementById('aaSkillGrid');
  if (!grid) return;

  if (q) {
    let totalMatches = 0;
    const sections = toolNames.map(tool => {
      const list = byTool[tool] || [];
      const filtered = list.filter(s => aaSkillMatches(s, q));
      if (!filtered.length) return '';
      totalMatches += filtered.length;
      return '<section class="aa-skill-tool-result">' +
        '<div class="aa-skill-tool-result-head"><span>' + aaToolEmoji(tool) + ' ' + escapeHtml(tool) + '</span><span>' + filtered.length + '</span></div>' +
        aaRenderSkillLevelSections(tool, list, filtered) +
      '</section>';
    }).join('');
    grid.innerHTML = totalMatches ? sections : '<div style="color:var(--slate);font-size:13px">' + escapeHtml(labels.noMatches) + '</div>';
  } else {
    const list = byTool[_aaSkillTab] || [];
    grid.innerHTML = list.length ? aaRenderSkillLevelSections(_aaSkillTab, list, list) : '<div style="color:var(--slate);font-size:13px">' + escapeHtml(labels.noSkillsData) + '</div>';
  }

  window._aaSkillsData = skills;
}

function openSkillModal(tab, idx) {
  const labels = aiAssetsText();
  console.log('[SkillModal] tab:', tab, 'idx:', idx);
  const skills = window._aaSkillsData || _aaState.data?.skills || {};
  const list = (skills.byTool || {})[tab] || [];
  const s = list[idx];
  console.log('[SkillModal] list.length:', list.length, 'skill:', s?.name);
  if (!s) { console.error('[SkillModal] skill not found'); return; }

  const desc = s.description || labels.noDescription;
  const path = s.path || '';
  const source = s.source || '';
  const level = s.levelLabel || s.level || s.type || '';
  const sourceKind = s.sourceKindLabel || s.sourceKind || '';
  const category = s.category || '';
  const profile = s.profile || '';

  let html = '<div style="padding:4px 0">';
  html += '<div style="font-size:14px;font-weight:700;color:var(--navy);margin-bottom:6px">' + escapeHtml(s.name) + '</div>';
  html += '<div style="font-size:11px;color:var(--slate);margin-bottom:16px">' + escapeHtml(level) + (profile ? ' · ' + escapeHtml(labels.profileLabel) + ': ' + escapeHtml(profile) : '') + (sourceKind ? ' · ' + escapeHtml(labels.sourceKindLabel) + ': ' + escapeHtml(sourceKind) : '') + ' · ' + (s.type||'') + ' · ' + (s.lastModified||'') + ' · ' + escapeHtml(source) + (category ? ' · ' + escapeHtml(category) : '') + '</div>';
  html += '<div style="font-size:13px;color:#334155;line-height:1.7;white-space:pre-wrap;margin-bottom:20px">' + escapeHtml(desc) + '</div>';
  if (path) {
    html += '<div style="border-top:1px solid var(--border);padding-top:14px;display:flex;justify-content:flex-end">';
    html += '<button data-aa-skill-edit="1" data-aa-skill-source="' + escapeHtml(source) + '" data-aa-skill-id="' + escapeHtml(s.id||s.name) + '" data-aa-skill-path="' + escapeHtml(path) + '" style="padding:8px 20px;background:var(--purple);color:white;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer">' + escapeHtml(labels.editSkill) + '</button>';
    html += '</div>';
  }
  html += '</div>';
  _showModalContent(html, aaToolEmoji(tab) + ' ' + s.name);
}

function aaToolEmoji(name) {
  const map = {'OpenClaw':'🦞','Claude Code':'✳️','Gemini CLI':'✨','Codex':'🤖','Hermes':'⚕️','OpenCode':'🐙','Antigravity':'🛡️','Cursor':'🖱️'};
  return map[name] || '🛠️';
}

async function openSkillDoc(source, skillId, skillPath) {
  // skillPath is a directory for OpenClaw skills, file for Claude Code commands
  let filePath = skillPath;
  // For directories, look for SKILL.md or the first .md file
  if (filePath && !filePath.endsWith('.md') && !filePath.endsWith('.json') && !filePath.endsWith('.toml')) {
    // It's a directory — try SKILL.md
    filePath = filePath.replace(/\/$/, '') + '/SKILL.md';
  }
  await openDocModal(source, skillId, '', filePath);
}

// ═══════════════════════════════════════════════════════
// Section K: Tool Configs
// ═══════════════════════════════════════════════════════
function renderToolConfigs(configs) {
  const labels = aiAssetsText();
  const container = document.getElementById('aaToolConfigs');
  const summary = document.getElementById('aaToolConfigSummary');
  if (!container) return;
  if (!configs.length) {
    if (summary) summary.innerHTML = '';
    container.innerHTML = '<div class="aa-toolconfig-empty">' + escapeHtml(labels.noToolConfigData) + '</div>';
    return;
  }

  const activeCount = configs.filter(c => c.status === 'detected' || c.status === 'active').length;
  const listenCount = configs.filter(c => (c.ports || []).length).length;
  const lastChecked = configs.map(c => c.checkedAt || '').sort().reverse()[0] || '—';
  if (summary) {
    summary.innerHTML = [
      [labels.detectedTools, activeCount + ' / ' + configs.length],
      [labels.listeningServices, listenCount],
      [labels.lastChecked, lastChecked],
    ].map(item => '<div class="aa-toolconfig-metric"><span>' + item[0] + '</span><strong>' + escapeHtml(String(item[1])) + '</strong></div>').join('');
  }

  container.innerHTML = configs.map(c => {
    const portText = (c.ports || []).length ? c.ports.join(', ') : labels.noListeningPorts;
    return '<article class="aa-toolconfig-row-card">' +
      '<div class="aa-toolconfig-identity">' +
        '<div class="aa-toolconfig-icon">' + (c.emoji || '') + '</div>' +
        '<div class="aa-toolconfig-intro"><div class="aa-toolconfig-name">' + escapeHtml(c.name) + '</div><span class="aa-toolconfig-status ' + escapeHtml(c.status || '') + '">' + escapeHtml(c.status || '') + '</span></div>' +
      '</div>' +
      '<div class="aa-toolconfig-runtime">' +
        '<div class="aa-toolconfig-version">' + escapeHtml(c.version || labels.versionUnknown) + '</div>' +
        '<div class="aa-toolconfig-port ' + ((c.ports || []).length ? 'listening' : '') + '">' + escapeHtml(portText) + '</div>' +
      '</div>' +
      '<div class="aa-toolconfig-paths">' +
        aaToolConfigPath(labels.runPath, c.path || '—') +
        aaToolConfigPath(labels.configPath, c.configPath || '—') +
        aaToolConfigPath(labels.executablePath, c.executablePath || '—') +
      '</div>' +
      '<div class="aa-toolconfig-audit"><span>' + escapeHtml(labels.configUpdated) + '</span><strong>' + escapeHtml(c.updatedAt || '—') + '</strong><span>' + escapeHtml(labels.checkedAt) + '</span><strong>' + escapeHtml(c.checkedAt || '—') + '</strong></div>' +
    '</article>';
  }).join('');
}

function aaToolConfigPath(key, value) {
  return '<div class="aa-toolconfig-path-line"><span>' + escapeHtml(key) + '</span><code title="' + escapeHtml(String(value)) + '">' + escapeHtml(String(value)) + '</code></div>';
}

async function refreshToolConfigDiscovery() {
  const labels = aiAssetsText();
  const button = document.getElementById('aaToolDetectBtn');
  if (button) { button.disabled = true; button.textContent = labels.detecting; }
  try {
    const discoveryResponse = await fetch('/api/settings/external-tools/rediscover', { method: 'POST' });
    const discovery = await discoveryResponse.json();
    if (!discoveryResponse.ok || discovery.error) throw new Error(discovery.error || labels.pathDetectFailed);
    openModal(labels.toolRediscovery, renderExternalToolRediscoveryModal(discovery));
    const response = await fetch('/api/ai-assets/tool-configs/discover', { method: 'POST' });
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || labels.detectionFailed);
    if (_aaState.data) _aaState.data.toolConfigs = result.toolConfigs || [];
    renderToolConfigs(result.toolConfigs || []);
  } catch (error) {
    console.error('Tool config discovery failed', error);
  } finally {
    if (button) { button.disabled = false; button.textContent = labels.rediscover; }
  }
}

document.getElementById('aaToolDetectBtn')?.addEventListener('click', refreshToolConfigDiscovery);

function renderExternalToolRediscoveryModal(data, message) {
  const labels = aiAssetsText();
  const discoveries = data.discoveries || [];
  const catalog = ((data.catalog || {}).tools || []);
  const options = catalog.map(item => '<option value="' + escapeHtml(item.id) + '">' + escapeHtml((item.name || item.id)) + '</option>').join('');
  const rows = discoveries.length ? discoveries.map(item => {
    const action = (item.status === 'new' || item.status === 'changed')
      ? '<button class="wr-export-btn" onclick="addDetectedExternalTool(\'' + escapeJs(item.tool) + '\', \'' + escapeJs(item.instanceId) + '\', \'' + escapeJs(item.path) + '\')">' + escapeHtml(labels.writeSettings) + '</button>'
      : '<span class="settings-runtime-chip ok">' + escapeHtml(labels.matched) + '</span>';
    return '<tr><td>' + escapeHtml(item.name || item.tool) + '</td><td>' + escapeHtml(item.instanceId || item.tool) + '</td><td>' + escapeHtml(item.status || '') + '</td><td><code>' + escapeHtml(item.path || '') + '</code></td><td>' + action + '</td></tr>';
  }).join('') : '<tr><td colspan="5">' + escapeHtml(labels.noSupportedToolDirs) + '</td></tr>';
  return '<div class="settings-section">' +
    (message ? '<div class="settings-runtime-line"><b>' + escapeHtml(message) + '</b></div>' : '') +
    '<div class="settings-note">' + escapeHtml(labels.rediscoveryNote) + '</div>' +
    '<table class="settings-table"><thead><tr><th>' + escapeHtml(labels.tool) + '</th><th>' + escapeHtml(labels.instance) + '</th><th>' + escapeHtml(labels.status) + '</th><th>' + escapeHtml(labels.path) + '</th><th>' + escapeHtml(labels.action) + '</th></tr></thead><tbody>' + rows + '</tbody></table>' +
    '<div class="settings-path-group"><div class="settings-path-group-title">' + escapeHtml(labels.manualAddTool) + '</div>' +
      '<div class="settings-row"><label>' + escapeHtml(labels.tool) + '</label><select id="externalToolManualTool">' + options + '</select></div>' +
      '<div class="settings-row"><label>' + escapeHtml(labels.instanceName) + '</label><input id="externalToolManualInstance" placeholder="' + escapeHtml(labels.instancePlaceholder) + '"></div>' +
      '<div class="settings-row"><label>' + escapeHtml(labels.path) + '</label><input id="externalToolManualPath" placeholder="~/.openclaw-2"></div>' +
      '<div class="settings-actions"><span class="settings-status" id="externalToolAddStatus"></span><button class="wr-export-btn" onclick="addManualExternalTool()">' + escapeHtml(labels.addTool) + '</button></div>' +
    '</div>' +
  '</div>';
}

async function addDetectedExternalTool(tool, instanceId, path) {
  await addExternalToolPath({ tool, instanceId, path });
}

async function addManualExternalTool() {
  await addExternalToolPath({
    tool: document.getElementById('externalToolManualTool')?.value || '',
    instanceId: document.getElementById('externalToolManualInstance')?.value || '',
    path: document.getElementById('externalToolManualPath')?.value || '',
  });
}

async function addExternalToolPath(payload) {
  const labels = aiAssetsText();
  const status = document.getElementById('externalToolAddStatus');
  if (status) status.textContent = labels.writing;
  try {
    const response = await fetch('/api/settings/external-tools/add', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || labels.writeFailed);
    const added = result.added || payload.instanceId || payload.tool;
    if (status) status.textContent = labels.writtenRefreshing(added);
    await refreshExternalToolDiscoveryViews(labels.written(added));
  } catch (error) {
    if (status) status.textContent = labels.writeFailed + ': ' + error.message;
  }
}

async function refreshExternalToolDiscoveryViews(message) {
  const labels = aiAssetsText();
  const discoveryResponse = await fetch('/api/settings/external-tools/rediscover', { method: 'POST' });
  const discovery = await discoveryResponse.json();
  if (!discoveryResponse.ok || discovery.error) throw new Error(discovery.error || labels.pathRediscoveryFailed);
  const body = document.getElementById('modal-body');
  if (body) body.innerHTML = renderExternalToolRediscoveryModal(discovery, message);
  const response = await fetch('/api/ai-assets/tool-configs/discover', { method: 'POST' });
  const result = await response.json();
  if (!response.ok || result.error) throw new Error(result.error || labels.toolConfigRefreshFailed);
  if (_aaState.data) _aaState.data.toolConfigs = result.toolConfigs || [];
  renderToolConfigs(result.toolConfigs || []);
}

// ═══════════════════════════════════════════════════════
// Agent Document Modal
// ═══════════════════════════════════════════════════════
let _aaDocState = { agentName: '', fileName: '', workspace: '', path: '', original: '', createable: false };

async function openDocModal(agentName, fileName, workspace, fullPath, createable) {
  const labels = aiAssetsText();
  const filePath = fullPath || (workspace ? workspace.replace(/\/$/, '') + '/' + fileName : fileName);
  _aaDocState = { agentName, fileName, workspace, path: filePath, original: '', createable: !!createable };
  const modal = document.getElementById('aaDocModal');
  const title = document.getElementById('aaDocModalTitle');
  const subtitle = document.getElementById('aaDocModalSubtitle');
  const backBtn = document.getElementById('aaModalBack');
  const textarea = document.getElementById('aaDocTextarea');
  const content = document.getElementById('aaModalContent');
  if (!modal || !title || !textarea || !content) {
    console.error('AI assets modal DOM is incomplete');
    return;
  }
  if (modal.parentElement !== document.body) document.body.appendChild(modal);

  // Editor mode: wider modal, textarea fills body
  modal.classList.add('aa-editing');
  const panel = modal.querySelector('.aa-modal');
  const body = modal.querySelector('.aa-modal-body');
  if (panel) {
    panel.style.setProperty('width', 'min(1100px, calc(100vw - 48px))', 'important');
    panel.style.setProperty('height', 'min(96vh, 1200px)', 'important');
    panel.style.setProperty('max-height', 'min(96vh, 1200px)', 'important');
  }
  if (body) {
    body.style.setProperty('flex', '1', 'important');
    body.style.setProperty('display', 'flex', 'important');
    body.style.setProperty('flex-direction', 'column', 'important');
    body.style.setProperty('min-height', '0', 'important');
  }

	  title.textContent = agentName + ' · ' + fileName;
	  if (createable) title.textContent = labels.createFileTitle(agentName, fileName);
  if (subtitle) subtitle.textContent = filePath;
  if (backBtn) backBtn.style.display = _aaModalBackAction ? 'inline-flex' : 'none';
  content.style.display = 'none';
  textarea.style.display = 'block';
  textarea.style.setProperty('flex', '1', 'important');
  textarea.style.setProperty('min-height', 'calc(96vh - 134px)', 'important');
  textarea.value = labels.loadingFile;
  textarea.disabled = true;
  const footer = modal.querySelector('.aa-modal-footer');
  if (footer) footer.style.display = 'flex';
  modal.classList.add('active');
  modal.style.setProperty('display', 'flex', 'important');
  modal.style.setProperty('position', 'fixed', 'important');
  modal.style.setProperty('inset', '0', 'important');
  modal.style.setProperty('z-index', '999999', 'important');
  modal.style.setProperty('align-items', 'center', 'important');
  modal.style.setProperty('justify-content', 'center', 'important');
  modal.style.setProperty('background', 'rgba(6, 27, 49, 0.62)', 'important');

  try {
    const res = await fetch('/api/file-content?path=' + encodeURIComponent(filePath));
	    if (!res.ok) {
	      if (res.status === 404 && createable) {
	        textarea.value = '';
	        _aaDocState.original = '';
	        textarea.disabled = false;
	        textarea.focus();
	        aaToast(labels.fileWillBeCreated, 'success');
	        return;
	      }
      throw new Error('HTTP ' + res.status);
    }
    const data = await res.json();
    textarea.value = data.content || '';
    _aaDocState.original = data.content || '';
  } catch (e) {
    textarea.value = labels.loadFileFailed + e.message;
  }
  textarea.disabled = false;
}

function closeDocModal() {
  const modal = document.getElementById('aaDocModal');
  if (!modal) return;
  modal.classList.remove('active', 'aa-editing');
  _aaModalBackAction = null;
  modal.style.setProperty('display', 'none', 'important');
  const content = document.getElementById('aaModalContent');
  const textarea = document.getElementById('aaDocTextarea');
  if (content) content.style.display = 'none';
	  if (textarea) {
	    textarea.style.display = 'block';
	    textarea.style.removeProperty('flex');
	    textarea.style.removeProperty('min-height');
	    textarea.disabled = false;
	  }
  const panel = modal.querySelector('.aa-modal');
  if (panel) {
    panel.style.removeProperty('width');
    panel.style.removeProperty('height');
    panel.style.removeProperty('max-height');
  }
  const body = modal.querySelector('.aa-modal-body');
  if (body) {
    body.style.removeProperty('display');
    body.style.removeProperty('flex-direction');
  }
  const footer = modal.querySelector('.aa-modal-footer');
  if (footer) footer.style.display = 'flex';
  const subtitle = document.getElementById('aaDocModalSubtitle');
  if (subtitle) subtitle.textContent = '';
  const backBtn = document.getElementById('aaModalBack');
  if (backBtn) backBtn.style.display = 'none';
}

async function saveDoc() {
  const labels = aiAssetsText();
  const textarea = document.getElementById('aaDocTextarea');
  const content = textarea.value;
  const filePath = _aaDocState.path || (_aaDocState.workspace ? _aaDocState.workspace.replace(/\/$/, '') + '/' + _aaDocState.fileName : _aaDocState.fileName);
  const confirmationText = 'SAVE ACTANARA FILE';
  const typed = prompt(labels.savePrompt + confirmationText);
  if (typed !== confirmationText) {
    aaToast(labels.saveCancelled, 'error');
    return;
  }

  try {
    const res = await fetch('/api/file-content', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath, content, confirmationText }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (data.success) {
      _aaDocState.original = content;
      aaToast(labels.saveSuccess + (data.backupPath ? ' · ' + labels.backupLabel : ''), 'success');
    } else {
      throw new Error(data.error || data.message || labels.saveFailed);
    }
  } catch (e) {
    aaToast('❌ ' + e.message, 'error');
  }
}

function aaToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'aa-toast aa-toast-' + (type || 'success');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

// ESC to close modal
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDocModal();
});

// Auto-load AI assets when page becomes visible
const _aaObserver = new MutationObserver(() => {
  const page = document.getElementById('page-static');
  if (page && page.classList.contains('active') && document.getElementById('aiAssetsContent')?.style.display !== 'block' && !document.getElementById('aiAssetsLoading')?.innerHTML.startsWith('❌')) {
    loadAiAssets();
  }
});
_aaObserver.observe(document.getElementById('page-static') || document.body, { attributes: true, attributeFilter: ['class'] });

fetchTokenClock();
setInterval(fetchTokenClock, 30000);
loadDiaryNav();


// ═══════════════════════════════════════════════════════
// SSE 实时数据连接（指数退避重连）
// ═══════════════════════════════════════════════════════
let sseTokenConn = null;
let sseAgentConn = null;
let sseRetryDelay = 1000;
const sseMaxDelay = 30000;
const pageLoadTime = Date.now();

function formatTokens(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function formatUptime(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return h + 'h ' + String(m).padStart(2, '0') + 'm';
  return m + 'm ' + String(sec).padStart(2, '0') + 's';
}

function flashElement(id, newVal) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.textContent !== String(newVal)) {
    el.textContent = newVal;
    el.classList.remove('flash-update');
    void el.offsetWidth; // trigger reflow
    el.classList.add('flash-update');
  }
}

function updateTokenCards(data) {
  const labels = dashboardShellText();
  // today summary
  const summary = data.summary || {};
  const today = data.today || {};
  let totalMsgs = 0;
  for (const [agent, v] of Object.entries(today)) {
    totalMsgs += (v.count || 0);
  }

  const el = (id) => document.getElementById(id);
  flashElement('statMsgCount', totalMsgs.toLocaleString());
  if (el('statMsgTrend')) el('statMsgTrend').textContent = labels.todayDetails;

  const totalTokens = summary.total ?? ((summary.input || 0) + (summary.output || 0) + (summary.cacheRead || 0));
  flashElement('statTodayTokens', formatTokens(totalTokens));
  if (el('statTokenTrend')) el('statTokenTrend').textContent = labels.protocolTotal + ' · ' + labels.input + ' ' + formatTokens(summary.input) + ' · ' + labels.output + ' ' + formatTokens(summary.output);

  const hitRate = summary.cacheHitRate;
  flashElement('statCacheHit', hitRate != null ? hitRate.toFixed(1) + '%' : '—');
  if (el('statCacheTrend')) el('statCacheTrend').textContent = 'cacheRead ' + formatTokens(summary.cacheRead || 0);

  flashElement('lastUpdateTime', (data.updatedAt || new Date().toLocaleTimeString()));

  // Update uptime
  if (el('statUptime')) el('statUptime').textContent = formatUptime(Date.now() - pageLoadTime);
}

// ── Task Board (from /api/tasks) ─────────────────────────────────────────────
let _cachedTasks = [];

function taskBoardText() {
  return dashboardLanguageProfile() === 'en'
    ? {
        completed: 'Completed Milestones',
        planned: 'Planned',
        active: 'In Progress',
        unknown: 'Unknown',
        projects: 'projects',
        activeShort: 'In progress',
        plannedShort: 'Planned',
        milestoneShort: 'Milestones',
        noData: 'No data',
        boardTitle: 'Task Board',
        summary: (count, lastUpdated) => `📋 Task Board · ${count} projects${lastUpdated ? ' · ' + lastUpdated : ''}`,
        sectionCount: (section, count) => `${section} (${count} projects)`,
        noSubtasks: 'No subtasks',
        connected: '🟢 Connected',
        reconnecting: (seconds) => `🔴 Reconnecting ${seconds}s`,
      }
    : {
        completed: '已完成里程碑',
        planned: '已计划（待启动）',
        active: '进行中',
        unknown: '未知',
        projects: '个项目',
        activeShort: '进行中',
        plannedShort: '已计划',
        milestoneShort: '里程碑',
        noData: '暂无数据',
        boardTitle: '任务看板',
        summary: (count, lastUpdated) => `📋 任务看板 · 共 ${count} 个项目${lastUpdated ? ' · ' + lastUpdated : ''}`,
        sectionCount: (section, count) => `${section}（${count} 个项目）`,
        noSubtasks: '暂无子任务',
        connected: '🟢 已连接',
        reconnecting: (seconds) => `🔴 重连 ${seconds}s`,
      };
}

function novaTaskSection(status) {
  const labels = taskBoardText();
  if (status === 'completed') return labels.completed;
  if (status === 'planned') return labels.planned;
  if (status === 'active' || status === 'blocked') return labels.active;
  return labels.unknown;
}

function novaTaskNodesFromPayload(data) {
  if (!data || typeof data !== 'object') return [];
  if (Array.isArray(data.nodes)) return data.nodes;
  const roots = Array.isArray(data.tree) ? data.tree : [];
  const result = [];
  const visit = (node, parentNodeId) => {
    if (!node || typeof node !== 'object') return;
    const copy = Object.assign({}, node);
    if (parentNodeId && !copy.parentNodeId) copy.parentNodeId = parentNodeId;
    result.push(copy);
    for (const child of (Array.isArray(node.children) ? node.children : [])) {
      visit(child, node.nodeId);
    }
  };
  roots.forEach(node => visit(node, null));
  return result;
}

function novaTaskTreeToDashboardTasks(data) {
  const nodes = novaTaskNodesFromPayload(data).filter(node => {
    const status = node.status || node.taskStatus;
    return ['active', 'planned', 'blocked', 'completed'].includes(status);
  });
  if (!nodes.length) return [];
  const byParent = new Map();
  nodes.forEach(node => {
    const parentId = node.parentNodeId || null;
    if (!byParent.has(parentId)) byParent.set(parentId, []);
    byParent.get(parentId).push(node);
  });
  const roots = nodes.filter(node => !node.parentNodeId || !nodes.some(parent => parent.nodeId === node.parentNodeId));
  return roots.map(root => {
    const children = byParent.get(root.nodeId) || [];
    const subtasks = (children.length ? children : [root]).map(item => {
      const status = item.status || item.taskStatus;
      return {
        content: item.title || item.nodeId || 'Untitled task',
        done: status === 'completed',
        nodeId: item.nodeId,
        status,
      };
    });
    return {
      project: root.title || root.nodeId || 'Untitled task',
      section: novaTaskSection(root.status || root.taskStatus),
      nodeId: root.nodeId,
      subtasks,
    };
  });
}

function dashboardTasksFromPayload(data) {
  const v2Tasks = novaTaskTreeToDashboardTasks(data);
  if (v2Tasks.length) return v2Tasks;
  return Array.isArray(data && data.tasks) ? data.tasks : [];
}

function updateTaskBoard(data) {
  const labels = taskBoardText();
  const tasks = dashboardTasksFromPayload(data);
  _cachedTasks = tasks;

  // Count by section
  let milestoneCount = 0, activeCount = 0, plannedCount = 0, pendingCount = 0;
  let activeSubtasks = 0, plannedSubtasks = 0;
  for (const t of tasks) {
    const s = t.section || '';
    const pending = t.subtasks.filter(s => !s.done).length;
    const done = t.subtasks.filter(s => s.done).length;
    if (s.includes('里程碑') || s.includes('Milestone')) {
      milestoneCount += done;
    } else if (s.includes('进行中') || s.includes('In Progress')) {
      activeCount++;
      activeSubtasks += pending;
    } else if (s.includes('已计划') || s.includes('Planned')) {
      plannedCount++;
      plannedSubtasks += pending;
    } else {
      pendingCount += pending;
    }
  }

  const el = id => document.getElementById(id);
  if (el('tb-milestone-count')) el('tb-milestone-count').textContent = milestoneCount;
  if (el('tb-active-count'))    el('tb-active-count').textContent    = activeCount;
  if (el('tb-planned-count'))   el('tb-planned-count').textContent   = plannedCount;
  if (el('tb-pending-count'))   el('tb-pending-count').textContent   = pendingCount || '—';

  if (el('statTaskTotal')) el('statTaskTotal').textContent = dashboardLanguageProfile() === 'en' ? tasks.length + ' ' + labels.projects : tasks.length + ' ' + labels.projects;
  if (el('statTaskTrend')) {
    const parts = [];
    if (activeCount)   parts.push(labels.activeShort + ' ' + activeCount);
    if (plannedCount)  parts.push(labels.plannedShort + ' ' + plannedCount);
    if (milestoneCount) parts.push(labels.milestoneShort + ' ' + milestoneCount);
    el('statTaskTrend').textContent = parts.join(' · ') || labels.noData;
  }
}

// Global store for showAllTasks modal
window._taskBoardData = null;
function showAllTasks() {
  const labels = taskBoardText();
  const tasks = window._taskBoardData || [];
  const lastUpdated = document.getElementById('statTaskTrend') ?
    document.getElementById('statTaskTrend').textContent : '';

  // Group by section
  const grouped = {};
  for (const t of tasks) {
    const s = t.section || labels.unknown;
    if (!grouped[s]) grouped[s] = [];
    grouped[s].push(t);
  }

  const sectionOrder = Array.from(new Set([labels.active, labels.planned, labels.completed, labels.unknown, '进行中', '已计划（待启动）', '已完成里程碑', '未知']));
  const sectionColors = {
    [labels.active]: '#f59e0b', [labels.planned]: '#3b82f6',
    [labels.completed]: 'var(--purple)', [labels.unknown]: 'var(--slate)',
    '进行中': '#f59e0b', '已计划（待启动）': '#3b82f6',
    '已完成里程碑': 'var(--purple)', '未知': 'var(--slate)',
  };
  const sectionEmoji = { [labels.active]: '🟡', [labels.planned]: '📋', [labels.completed]: '✅', [labels.unknown]: '❓', '进行中': '🟡', '已计划（待启动）': '📋', '已完成里程碑': '✅', '未知': '❓' };

  let content = '<p style="color:var(--gray);font-size:12px;margin-bottom:16px">' +
    escapeHtml(labels.summary(tasks.length, lastUpdated)) + '</p>';

  for (const section of sectionOrder) {
    if (!grouped[section] || grouped[section].length === 0) continue;
    const color = sectionColors[section] || 'var(--slate)';
    const emoji = sectionEmoji[section] || '';
    content += '<div style="margin-bottom:20px">';
    content += '<div style="font-size:12px;font-weight:700;color:' + color + ';margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid rgba(0,0,0,0.08)">' +
      emoji + ' ' + escapeHtml(labels.sectionCount(section, grouped[section].length)) + '</div>';

    for (const proj of grouped[section]) {
      content += '<div style="background:var(--light-bg);border-radius:8px;padding:10px 12px;margin-bottom:8px">';
      content += '<div style="font-size:13px;font-weight:600;margin-bottom:6px;color:var(--dark)">📁 ' + escapeHtml(proj.project) + '</div>';
      if (proj.subtasks.length === 0) {
        content += '<div style="font-size:12px;color:var(--gray);font-style:italic">' + escapeHtml(labels.noSubtasks) + '</div>';
      }
      for (const s of proj.subtasks) {
        const checkbox = s.done ? '☑️' : '☐';
        const strike = s.done ? 'text-decoration:line-through;opacity:0.6' : '';
        content += '<div style="font-size:12px;padding:2px 0;' + strike + '">' +
          checkbox + ' ' + escapeHtml(s.content) + '</div>';
      }
      content += '</div>';
    }
    content += '</div>';
  }

  openModal('📋 ' + labels.boardTitle, content);
}


const ACTANARA_SSE_STREAM_STATES = new Map();

function sseSourceWarnings(payload) {
  const state = payload && typeof payload.dashboardState === 'object'
    ? payload.dashboardState
    : {};
  const errors = Array.isArray(state.sourceErrors) ? state.sourceErrors : [];
  const warnings = errors.map(item => {
    if (!item || typeof item !== 'object') return '';
    const source = String(item.source || '').trim();
    const code = String(item.code || '').trim();
    return source && code ? source + ': ' + code : code || source;
  }).filter(Boolean);
  if (!warnings.length && ['error', 'unavailable', 'degraded'].includes(state.status)) {
    warnings.push(String(state.status));
  }
  return warnings;
}

function updateSseStreamState(label, patch) {
  const key = String(label || 'stream');
  const previous = ACTANARA_SSE_STREAM_STATES.get(key) || {
    transport: 'connecting',
    retrySeconds: 0,
    sourceWarnings: [],
  };
  ACTANARA_SSE_STREAM_STATES.set(key, {...previous, ...(patch || {})});
  renderSseConnectionStatus();
}

function renderSseConnectionStatus() {
  const el = document.getElementById('sseStatus');
  if (!el) return;

  const states = Array.from(ACTANARA_SSE_STREAM_STATES.values());
  const pending = states.filter(state => state.transport !== 'connected');
  const retrying = pending.find(state => state.transport === 'reconnecting');
  if (!states.length || pending.length) {
    if (retrying) {
      el.textContent = taskBoardText().reconnecting(Number(retrying.retrySeconds || 1));
      el.style.color = 'var(--ruby)';
    } else {
      el.textContent = dashboardShellText().sseConnecting;
      el.style.color = 'var(--slate)';
    }
  } else {
    el.textContent = taskBoardText().connected;
    el.style.color = 'var(--success)';
  }

  const warnings = Array.from(new Set(states.reduce((all, state) => {
    return all.concat(Array.isArray(state.sourceWarnings) ? state.sourceWarnings : []);
  }, [])));
  el.dataset.sourceHealth = warnings.length ? 'degraded' : 'ready';
  const warningPrefix = dashboardLanguageProfile() === 'en' ? 'Data source warning: ' : '数据源告警：';
  const accessibleText = warnings.length
    ? el.textContent + '; ' + warningPrefix + warnings.join(', ')
    : el.textContent;
  el.title = accessibleText;
  el.setAttribute('aria-label', accessibleText);
}

function connectSSE(url, onData, label) {
  let retryDelay = 1000;
  const maxDelay = 30000;

  function connect() {
    updateSseStreamState(label, {transport: 'connecting', retrySeconds: 0});
    const es = new EventSource(url);

    es.onopen = () => {
      updateSseStreamState(label, {transport: 'connected', retrySeconds: 0});
      retryDelay = 1000;
    };

    es.onmessage = (e) => {
      retryDelay = 1000;
      try {
        const data = JSON.parse(e.data);
        updateSseStreamState(label, {transport: 'connected', sourceWarnings: sseSourceWarnings(data)});
        onData(data);
      } catch (err) {
        console.error('SSE parse error:', err);
      }
    };

    es.onerror = () => {
      es.close();
      updateSseStreamState(label, {
        transport: 'reconnecting',
        retrySeconds: Math.round(retryDelay / 1000),
      });
      setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, maxDelay);
    };

    return es;
  }

  return connect();
}

// ── Initialize SSE connections ──
connectSSE('/events/tokens', updateTokenCards, 'tokens');
connectSSE('/events/tasks', (data) => { window._taskBoardData = dashboardTasksFromPayload(data); updateTaskBoard(data); }, 'tasks');

// ── Update uptime every second ──
setInterval(() => {
  const el = document.getElementById('statUptime');
  if (el) el.textContent = formatUptime(Date.now() - pageLoadTime);
}, 1000);

// ── Initial data fetch (fallback if SSE slow) ──
applyStaticDashboardText();
ensureDashboardLanguageProfile().then(profile => applyStaticDashboardText(profile)).catch(() => {});
refreshMsgbox();
setInterval(refreshMsgbox, 60000);
refreshBackgroundTaskButton();
setInterval(refreshBackgroundTaskButton, 60000);
loadDashboardTimezone();
fetch('/api/tokens').then(r => r.json()).then(data => {
  if (data) updateTokenCards(data);
}).catch(() => {});
// Initial tasks fetch (SSE fallback)
fetch('/api/tasks').then(r => r.json()).then(data => {
  if (!window._taskBoardData) {
    window._taskBoardData = dashboardTasksFromPayload(data);
    updateTaskBoard(data);
  }
}).catch(() => {});

// ── Hash-based Navigation (fallback + browser back/forward support) ──
initFromHash();
window.addEventListener('hashchange', showPageFromHash);

// ── Global event delegation for skill capsules ──
document.addEventListener('click', function(e) {
  const agentRow = e.target.closest('[data-aa-agent-row]');
  if (agentRow) {
    e.preventDefault();
    openAaAgentRowModal(Number(agentRow.dataset.aaAgentRow));
    return;
  }

  const toolCard = e.target.closest('[data-aa-agent-tool]:not([data-aa-agent-item])');
  if (toolCard && toolCard.classList.contains('aa-ap-tool-card')) {
    e.preventDefault();
    openApToolModal(Number(toolCard.dataset.aaAgentTool));
    return;
  }

  const agentItem = e.target.closest('[data-aa-agent-item]');
  if (agentItem) {
    e.preventDefault();
    openApItemModal(Number(agentItem.dataset.aaAgentTool), Number(agentItem.dataset.aaAgentItem));
    return;
  }

	  const docLink = e.target.closest('[data-aa-doc-path]');
	  if (docLink) {
	    e.preventDefault();
	    const path = docLink.dataset.aaDocPath || '';
	    const labels = aiAssetsText();
	    const agent = docLink.dataset.aaDocAgent || labels.documentFallback;
	    const name = docLink.dataset.aaDocName || path.split('/').pop() || labels.fileFallback;
	    const createable = docLink.dataset.aaDocCreate === '1';
	    closeDocModal();
	    setTimeout(() => openDocModal(agent, name, '', path, createable), 120);
	    return;
	  }

  const skillEdit = e.target.closest('[data-aa-skill-edit]');
  if (skillEdit) {
    e.preventDefault();
    const source = skillEdit.dataset.aaSkillSource || 'Skill';
    const skillId = skillEdit.dataset.aaSkillId || 'SKILL.md';
    const skillPath = skillEdit.dataset.aaSkillPath || '';
    closeDocModal();
    setTimeout(() => openSkillDoc(source, skillId, skillPath), 120);
    return;
  }

  const cap = e.target.closest('.aa-skill-capsule');
  if (!cap) return;
  e.preventDefault();
  e.stopPropagation();
  openSkillModal(cap.dataset.tab, parseInt(cap.dataset.idx));
}, true);
function initFromHash() {
  if (location.hash && location.hash !== '#') {
    showPageFromHash();
  }
  // Otherwise the page-home.active in HTML will show by default
}
