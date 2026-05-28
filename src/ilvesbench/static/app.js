const statusBox = document.getElementById("statusBox");
const runsContainer = document.getElementById("runs");
const configPathInput = document.getElementById("configPath");
const workloadPathInput = document.getElementById("workloadPath");
const originalDatabaseSelect = document.getElementById("originalDatabase");
const newDatabaseInput = document.getElementById("newDatabase");
const schemasInput = document.getElementById("schemas");
const dbSelectionStatus = document.getElementById("dbSelectionStatus");
const postgresStatusBox = document.getElementById("postgresStatus");

let activePoll = null;
let activeTab = "setup";
let latestProfiles = null;
let latestLlmStatus = null;
let latestPostgresStatus = null;
let discoveredDatabases = [];
let lastAutoTargetDatabase = newDatabaseInput.value.trim();
let profileLoading = false;
let profileError = "";
let latestProfilesContext = "";

const WORKSPACE_TABS = [
  { id: "setup", label: "Setup", steps: ["llm_gateway", "inspect_source_schema", "extract_workload_logs"] },
  { id: "profile", label: "Profile", steps: ["inspect_source_schema", "collect_extended_metrics"] },
  { id: "normalize", label: "Normalize", steps: ["propose_3nf_schema"] },
  { id: "migrate", label: "Migrate", steps: ["create_target_schema", "migrate_data"] },
  { id: "workload", label: "Workload", steps: ["rewrite_queries", "suggest_summary_tables"] },
  { id: "physical", label: "Physical design", steps: ["optimize_indexes", "create_secondary_indexes", "tune_postgresql_conf"] },
  { id: "benchmark", label: "Benchmark", steps: ["run_pgbench_original", "run_pgbench_new"] },
  { id: "compare", label: "Compare", steps: ["compare_disk_usage"] },
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

function setStatus(message, isError = false) {
  statusBox.textContent = message;
  statusBox.dataset.error = isError ? "true" : "false";
}

function selectedSchemas() {
  return schemasInput.value
    .split(",")
    .map((schema) => schema.trim())
    .filter(Boolean);
}

function requestContext() {
  return {
    config_path: configPathInput.value.trim(),
    workload_path: workloadPathInput.value.trim(),
    original_database: originalDatabaseSelect.value.trim(),
    new_database: newDatabaseInput.value.trim(),
    schemas: selectedSchemas(),
  };
}

function requestContextKey() {
  const context = requestContext();
  return JSON.stringify({
    config_path: context.config_path,
    original_database: context.original_database,
    new_database: context.new_database,
    schemas: context.schemas,
  });
}

function selectedOriginalDatabase(run = null) {
  return originalDatabaseSelect.value.trim() || latestPostgresStatus?.original_database || run?.summary?.original_database || "";
}

function selectedTargetDatabase(run = null) {
  return newDatabaseInput.value.trim() || latestPostgresStatus?.new_database || run?.summary?.new_database || "";
}

function runMatchesSelection(run) {
  if (!run || !run.summary) return false;
  return run.summary.original_database === selectedOriginalDatabase()
    && run.summary.new_database === selectedTargetDatabase();
}

function selectionOnlyRun(sourceRun = null) {
  return {
    run_id: "",
    created_at: "",
    updated_at: "",
    status: profileLoading ? "running" : "pending",
    config_path: configPathInput.value.trim(),
    summary: {
      original_database: selectedOriginalDatabase(),
      new_database: selectedTargetDatabase(),
    },
    steps: [],
    artifacts: {},
    selectionOnly: true,
    previousRunId: sourceRun?.run_id || "",
  };
}

function updateDatabaseSelectionStatus() {
  const source = originalDatabaseSelect.value.trim() || "not selected";
  const target = newDatabaseInput.value.trim() || "not selected";
  const schemas = selectedSchemas();
  dbSelectionStatus.innerHTML = `
    <strong>Chosen source database:</strong> ${escapeHtml(source)}
    <span aria-hidden="true"> | </span>
    <strong>Target database:</strong> ${escapeHtml(target)}
    <span aria-hidden="true"> | </span>
    <strong>Schemas:</strong> ${escapeHtml(schemas.length ? schemas.join(", ") : "config default")}
  `;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function step(run, name) {
  return (run.steps || []).find((item) => item.name === name) || {};
}

function statusRank(status) {
  return {
    failed: 6,
    input_required: 5,
    running: 4,
    planned: 3,
    pending: 2,
    completed: 1,
  }[status] || 0;
}

function effectiveStep(run, name) {
  const item = step(run, name);
  if (name === "llm_gateway" && latestLlmStatus) {
    return {
      ...item,
      title: item.title || "Test LLM gateway",
      status: latestLlmStatus.status,
      details: {
        ...(item.details || {}),
        summary: latestLlmStatus.summary,
      },
      error: latestLlmStatus.status === "failed" ? latestLlmStatus.summary : null,
    };
  }
  if (name === "inspect_source_schema" && latestPostgresStatus?.status === "ok" && item.status !== "completed") {
    return {
      ...item,
      title: item.title || "Inspect source PostgreSQL schema",
      status: "completed",
      details: {
        ...(item.details || {}),
        summary: "PostgreSQL connection is available.",
      },
      error: null,
    };
  }
  return item;
}

function aggregateStatus(run, names, tabId = "") {
  if (tabId === "setup") {
    const llm = latestLlmStatus?.status || step(run, "llm_gateway").status || "pending";
    const postgres = latestPostgresStatus?.status === "ok" ? "completed" : step(run, "inspect_source_schema").status || "pending";
    const workload = step(run, "extract_workload_logs").status || "pending";
    const statuses = [llm, postgres, workload];
    if (statuses.includes("running")) return "running";
    if (statuses.includes("failed")) return "failed";
    if (statuses.includes("input_required")) return "input_required";
    if (statuses.includes("planned")) return "planned";
    if (statuses.every((status) => status === "completed")) return "completed";
    return statuses.sort((a, b) => statusRank(b) - statusRank(a))[0] || "pending";
  }
  const statuses = names.map((name) => step(run, name).status || "pending");
  if (statuses.includes("running")) return "running";
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("input_required")) return "input_required";
  if (statuses.includes("planned")) return "planned";
  if (statuses.every((status) => status === "completed")) return "completed";
  return statuses.sort((a, b) => statusRank(b) - statusRank(a))[0] || "pending";
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "0";
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat().format(number) : String(value);
}

function formatBytes(value) {
  const number = Number(value || 0);
  if (!number) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = number;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

function pill(label, status) {
  return `<span class="pill pill-${escapeHtml(status || "pending")}">${escapeHtml(label)}</span>`;
}

function metricCard(label, value, meta = "") {
  return `
    <article class="metric-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      ${meta ? `<small>${escapeHtml(meta)}</small>` : ""}
    </article>
  `;
}

function profileFromSchema(schema) {
  const tables = schema?.tables || [];
  const columnCount = tables.reduce((total, table) => total + (table.columns || []).length, 0);
  const fkCount = tables.reduce((total, table) => total + (table.foreign_keys || []).length, 0);
  const indexCount = tables.reduce((total, table) => total + (table.indexes || []).length, 0);
  const uniqueCount = tables.reduce((total, table) => total + (table.unique_constraints || []).length, 0);
  const missingKeyCount = tables.filter((table) => !(table.unique_constraints || []).length).length;
  const wideTableCount = tables.filter((table) => (table.columns || []).length >= 30).length;
  const clusters = abstractClustersFromSchema(tables);
  return {
    database: schema?.database || "",
    status: tables.length ? "completed" : "unavailable",
    summary: `${tables.length} tables, ${columnCount} columns, ${fkCount} relationships.`,
    counts: {
      tables: tables.length,
      columns: columnCount,
      foreign_keys: fkCount,
      unique_constraints: uniqueCount,
      indexes: indexCount,
      tables_without_primary_key: missingKeyCount,
      wide_tables: wideTableCount,
    },
    averages: {
      columns_per_table: tables.length ? Math.round((columnCount / tables.length) * 100) / 100 : 0,
      foreign_keys_per_table: tables.length ? Math.round((fkCount / tables.length) * 100) / 100 : 0,
    },
    clusters,
    risk_signals: [
      { label: "Tables without key evidence", value: missingKeyCount, severity: missingKeyCount ? "warning" : "ok" },
      { label: "Wide tables", value: wideTableCount, severity: wideTableCount ? "warning" : "ok" },
      { label: "Relationship density", value: tables.length ? Math.round((fkCount / tables.length) * 100) / 100 : 0, severity: "info" },
    ],
    storage: { total_relation_bytes: schema?.database_size_bytes || 0 },
    privacy: { abstracted: true, table_names_returned: false, column_names_returned: false },
  };
}

function abstractClustersFromSchema(tables) {
  const nodes = new Set(tables.map((table) => `${table.schema}.${table.name}`));
  const graph = new Map([...nodes].map((name) => [name, new Set()]));
  const columnCounts = new Map(tables.map((table) => [`${table.schema}.${table.name}`, (table.columns || []).length]));
  for (const table of tables) {
    const source = `${table.schema}.${table.name}`;
    for (const foreignKey of table.foreign_keys || []) {
      const target = foreignKey.referenced_table;
      if (graph.has(source) && graph.has(target)) {
        graph.get(source).add(target);
        graph.get(target).add(source);
      }
    }
  }
  const seen = new Set();
  const clusters = [];
  for (const node of [...graph.keys()].sort()) {
    if (seen.has(node)) continue;
    const stack = [node];
    const component = [];
    seen.add(node);
    while (stack.length) {
      const current = stack.pop();
      component.push(current);
      for (const neighbor of graph.get(current)) {
        if (!seen.has(neighbor)) {
          seen.add(neighbor);
          stack.push(neighbor);
        }
      }
    }
    const edges = component.reduce((total, item) => total + graph.get(item).size, 0) / 2;
    const columns = component.reduce((total, item) => total + (columnCounts.get(item) || 0), 0);
    clusters.push({
      label: `Cluster ${clusters.length + 1}`,
      table_count: component.length,
      relationship_count: edges,
      average_columns: component.length ? Math.round((columns / component.length) * 100) / 100 : 0,
      shape: edges / Math.max(component.length, 1) >= 1 ? "connected" : component.length > 1 ? "sparse" : "isolated",
    });
  }
  return clusters.sort((a, b) => b.table_count - a.table_count).slice(0, 12);
}

function targetProfileFromRun(run) {
  const createStep = step(run, "create_target_schema");
  const migrateStep = step(run, "migrate_data");
  const normalizeStep = step(run, "propose_3nf_schema");
  const targetTables = normalizeStep.details?.target_tables || [];
  const targetColumns = targetTables.reduce((total, table) => total + (table.columns || []).length, 0);
  return {
    database: selectedTargetDatabase(run) || "",
    status: createStep.status === "completed" ? "completed" : "missing",
    summary: createStep.status === "completed"
      ? `${targetTables.length} proposed tables, ${targetColumns} proposed columns.`
      : "Database has not been created yet.",
    counts: {
      tables: targetTables.length,
      columns: targetColumns,
      foreign_keys: targetTables.reduce((total, table) => total + (table.foreign_keys || []).length, 0),
      indexes: 0,
      estimated_rows: 0,
      data_populated: migrateStep.status === "completed" ? 1 : 0,
    },
    clusters: [],
    risk_signals: [
      { label: "Schema created", value: createStep.status === "completed" ? "yes" : "no", severity: createStep.status === "completed" ? "ok" : "warning" },
      { label: "Data migrated", value: migrateStep.status === "completed" ? "yes" : "no", severity: migrateStep.status === "completed" ? "ok" : "warning" },
    ],
  };
}

function pendingProfile(database, kind) {
  if (profileLoading) {
    return {
      database,
      status: "running",
      summary: `Profiling ${database} from PostgreSQL...`,
      counts: {},
      clusters: [],
      risk_signals: [
        { label: "Profile", value: "running", severity: "info" },
      ],
      storage: {},
    };
  }
  if (profileError) {
    return {
      database,
      status: "unavailable",
      summary: profileError,
      counts: {},
      clusters: [],
      risk_signals: [
        { label: "Profile", value: "failed", severity: "warning" },
      ],
      storage: {},
    };
  }
  return {
    database,
    status: kind === "target" ? "missing" : "pending",
    summary: kind === "target" ? `${database} has not been created or profiled yet.` : `${database} has not been profiled yet.`,
    counts: {},
    clusters: [],
    risk_signals: [],
    storage: {},
  };
}

async function fetchArtifact(runId, artifactName) {
  return api(`/api/runs/${runId}/artifacts/${artifactName}`);
}

async function loadCurrentArtifacts(run) {
  const artifacts = {};
  const names = ["inspect_source_schema", "extract_workload_logs", "collect_extended_metrics", "compare_disk_usage"];
  await Promise.all(
    names
      .filter((name) => run.artifacts?.[name])
      .map((name) =>
        fetchArtifact(run.run_id, name)
          .then((payload) => {
            artifacts[name] = payload;
          })
          .catch(() => {}),
      ),
  );
  return artifacts;
}

function renderTabs(run) {
  return `
    <nav class="workspace-tabs" aria-label="Run workflow">
      ${WORKSPACE_TABS.map((tab) => {
        const status = aggregateStatus(run, tab.steps, tab.id);
        return `
          <button class="tab-button tab-status-${escapeHtml(status)} ${activeTab === tab.id ? "is-active" : ""}" type="button" data-tab="${escapeHtml(tab.id)}">
            <span>${escapeHtml(tab.label)}</span>
            <small>${escapeHtml(status.replaceAll("_", " "))}</small>
          </button>
        `;
      }).join("")}
    </nav>
  `;
}

function renderDatabasePair(run, sourceProfile, targetProfile) {
  const originalFigure = databaseFigure("original", run, sourceProfile);
  const targetFigure = databaseFigure("target", run, targetProfile);
  return `
    <section class="database-pair">
      ${renderDatabaseFigure(originalFigure)}
      <article class="bridge-state">
        <span class="label">Flow</span>
        ${pill(`source workload ${originalFigure.workloadStatus}`, originalFigure.workloadReady ? "completed" : "planned")}
        ${pill(`${targetFigure.database} workload ${targetFigure.workloadStatus}`, targetFigure.workloadReady ? "completed" : "planned")}
      </article>
      ${renderDatabaseFigure(targetFigure)}
    </section>
  `;
}

function databaseFigure(kind, run, profile) {
  const counts = profile.counts || {};
  const sourceName = selectedOriginalDatabase(run) || profile.database || "source database";
  const targetName = selectedTargetDatabase(run) || profile.database || "target database";
  const createStep = step(run, "create_target_schema");
  const migrateStep = step(run, "migrate_data");
  const rewriteStep = step(run, "rewrite_queries");
  const indexStep = step(run, "optimize_indexes");
  const indexCreateStep = step(run, "create_secondary_indexes");
  const workloadStep = step(run, "extract_workload_logs");
  const originalBench = step(run, "run_pgbench_original");
  const newBench = step(run, "run_pgbench_new");
  const readiness = targetReadiness(run, profile);
  const exists = profile.status === "completed";
  const profiling = profile.status === "running";
  const hasRows = readiness.hasRows;
  const hasMatchingRun = !run.selectionOnly;
  const sourceWorkloadReady = hasMatchingRun && workloadStep.status === "completed" && Number(workloadStep.details?.statements_detected || 0) > 0;
  const targetWorkloadReady = hasMatchingRun && rewriteStep.status === "completed" && Boolean(rewriteStep.details?.workload_path);
  const noIndexWorkNeeded = indexStep.status === "completed" && Number(indexStep.details?.recommendation_count || 0) === 0;
  const targetIndexesReady = indexCreateStep.status === "completed" || noIndexWorkNeeded;

  if (kind === "original") {
    const ready = exists && sourceWorkloadReady && Boolean(originalBench.details?.workload_path);
    return {
      kind,
      title: "Source database",
      database: sourceName,
      headline: profiling ? `Profiling ${sourceName}` : exists ? `${sourceName} is available` : `${sourceName} is not profiled`,
      summary: profile.summary || "Source profile has not been collected yet.",
      ready,
      workloadReady: sourceWorkloadReady,
      workloadStatus: sourceWorkloadReady ? "available" : hasMatchingRun ? "missing" : "needs run",
      actions: originalActions(run),
      items: [
        { label: "Database", value: profiling ? "profiling" : exists ? "exists" : "not profiled", status: profiling ? "pending" : exists ? "ok" : "warning" },
        { label: "Data", value: profiling ? "estimating..." : hasRows ? `${formatNumber(counts.estimated_rows)} estimated rows` : exists ? "no rows observed" : "unknown", status: profiling ? "pending" : hasRows ? "ok" : "warning" },
        { label: "Indexes", value: profiling ? "counting..." : exists ? `${formatNumber(counts.indexes || 0)} found` : "unknown", status: profiling ? "pending" : Number(counts.indexes || 0) > 0 ? "ok" : "warning" },
        { label: "Workload", value: sourceWorkloadReady ? "available" : hasMatchingRun ? "missing" : "start a run", status: sourceWorkloadReady ? "ok" : "pending" },
        { label: "Benchmark", value: ready ? "ready" : "not ready", status: ready ? "ok" : "pending" },
      ],
    };
  }

  const ready = readiness.structureReady && readiness.dataReady && targetWorkloadReady && targetIndexesReady && Boolean(newBench.details?.workload_path);
  return {
    kind,
    title: "Target database",
    database: targetName,
    headline: profiling ? `Profiling ${targetName}` : exists ? `${targetName} exists` : `${targetName} not created`,
    summary: exists ? (profile.summary || "Target profile is available.") : "Structure, data, indexes, and workload will appear here as they are created.",
    ready,
    workloadReady: targetWorkloadReady,
    workloadStatus: targetWorkloadReady ? "created" : hasMatchingRun ? "not created" : "needs run",
    actions: targetActions(run, profile),
    items: [
      { label: "Database", value: profiling ? "profiling" : exists ? "exists" : "not created", status: profiling ? "pending" : exists ? "ok" : "missing" },
      { label: "Structure", value: profiling ? "checking..." : readiness.structureReady ? "created" : "not created", status: profiling ? "pending" : readiness.structureReady ? "ok" : "pending" },
      { label: "Data", value: profiling ? "estimating..." : readiness.dataReady ? "populated" : "not populated", status: profiling ? "pending" : readiness.dataReady ? "ok" : "pending" },
      { label: "Indexes", value: targetIndexesReady ? "created" : indexCreateStep.status === "planned" ? "planned" : "not created", status: targetIndexesReady ? "ok" : "pending" },
      { label: "Workload", value: targetWorkloadReady ? "created" : hasMatchingRun ? "not created" : "start a run", status: targetWorkloadReady ? "ok" : "pending" },
      { label: "Benchmark", value: ready ? "ready" : "not ready", status: ready ? "ok" : "pending" },
    ],
  };
}

function renderDatabaseFigure(figure) {
  return `
    <article class="database-figure database-figure-${escapeHtml(figure.kind)}">
      <header class="database-figure-head">
        <div>
          <span class="label">${escapeHtml(figure.title)}</span>
          <strong>${escapeHtml(figure.database)}</strong>
        </div>
        ${pill(figure.ready ? "benchmark ready" : "not ready", figure.ready ? "completed" : "planned")}
      </header>
      <div class="db-glyph" aria-hidden="true">
        <span></span>
      </div>
      <p class="database-headline">${escapeHtml(figure.headline)}</p>
      <p class="database-summary">${escapeHtml(figure.summary)}</p>
      <div class="state-checklist">
        ${figure.items.map((item) => `
          <div class="state-row state-${escapeHtml(item.status)}">
            <span>${escapeHtml(item.label)}</span>
            <strong>${escapeHtml(item.value)}</strong>
          </div>
        `).join("")}
      </div>
      ${figure.actions.length ? `<div class="database-actions">${figure.actions.join("")}</div>` : ""}
    </article>
  `;
}

function targetReadiness(run, profile) {
  const counts = profile.counts || {};
  const createStep = step(run, "create_target_schema");
  const migrateStep = step(run, "migrate_data");
  const exists = profile.status === "completed";
  const hasRows = Number(counts.estimated_rows || 0) > 0 || Number(counts.populated_tables || 0) > 0;
  return {
    exists,
    hasRows,
    structureReady: exists || createStep.status === "completed",
    dataReady: hasRows || migrateStep.status === "completed",
  };
}

function renderProfileCards(profile) {
  const counts = profile.counts || {};
  return `
    <div class="metric-grid">
      ${metricCard("Tables", formatNumber(counts.tables))}
      ${metricCard("Columns", formatNumber(counts.columns))}
      ${metricCard("Relationships", formatNumber(counts.foreign_keys))}
      ${metricCard("Indexes", formatNumber(counts.indexes))}
      ${metricCard("Rows", formatNumber(counts.estimated_rows))}
      ${metricCard("Key gaps", formatNumber(counts.tables_without_primary_key))}
      ${metricCard("Wide tables", formatNumber(counts.wide_tables))}
      ${metricCard("Storage", formatBytes(profile.storage?.total_relation_bytes || profile.storage?.database_size_bytes))}
    </div>
  `;
}

function renderClusters(profile) {
  const clusters = profile.clusters || [];
  if (!clusters.length) {
    return `<div class="empty-inline">No schema clusters available yet.</div>`;
  }
  return `
    <div class="cluster-list">
      ${clusters.map((cluster) => `
        <article class="cluster-row">
          <strong>${escapeHtml(cluster.label)}</strong>
          <span>${formatNumber(cluster.table_count)} tables</span>
          <span>${formatNumber(cluster.relationship_count)} links</span>
          <span>${escapeHtml(cluster.shape || "unknown")}</span>
        </article>
      `).join("")}
    </div>
  `;
}

function renderRiskSignals(profile) {
  const signals = profile.risk_signals || [];
  if (!signals.length) return "";
  return `
    <div class="signal-list">
      ${signals.map((signal) => `
        <article class="signal signal-${escapeHtml(signal.severity || "info")}">
          <span>${escapeHtml(signal.label)}</span>
          <strong>${escapeHtml(signal.value)}</strong>
        </article>
      `).join("")}
    </div>
  `;
}

function actionButton(run, action, label, tone = "", disabledReason = "") {
  const effectiveReason = disabledReason || (!run.run_id ? "Start a run for this database pair first." : "");
  const disabled = effectiveReason ? "disabled" : "";
  const title = effectiveReason ? ` title="${escapeHtml(effectiveReason)}"` : "";
  const reason = effectiveReason ? `<small>${escapeHtml(effectiveReason)}</small>` : "";
  return `
    <button class="action-button ${tone}" type="button" data-action="${escapeHtml(action)}" data-run-id="${escapeHtml(run.run_id)}" ${disabled}${title}>
      <span>${escapeHtml(label)}</span>
      ${reason}
    </button>
  `;
}

function originalActions(run) {
  const actions = [];
  const originalBench = step(run, "run_pgbench_original");
  const sourceName = selectedOriginalDatabase(run) || "source database";
  const disabledReason = run.selectionOnly
    ? `Start a run for ${sourceName} first.`
    : originalBench.details?.workload_path
    ? ""
    : "Needs a source workload before pgbench can run.";
  actions.push(actionButton(run, "run-pgbench-original", `Run pgbench on ${sourceName}`, "primary-approval", disabledReason));
  return actions;
}

function targetActions(run, profile = latestProfiles?.target || {}) {
  const actions = [];
  const targetName = selectedTargetDatabase(run) || "target database";
  const createStep = step(run, "create_target_schema");
  const migrateStep = step(run, "migrate_data");
  const rewriteStep = step(run, "rewrite_queries");
  const normalizationStep = step(run, "propose_3nf_schema");
  const workloadStep = step(run, "extract_workload_logs");
  const indexRecommendStep = step(run, "optimize_indexes");
  const indexCreateStep = step(run, "create_secondary_indexes");
  const newBench = step(run, "run_pgbench_new");
  const hasCreateSql = (createStep.details?.sql_statements || []).length > 0;
  const hasMigrationSql = (migrateStep.details?.statements || []).length > 0;
  const indexSqlCount = secondaryIndexSqlStatements(run).length;
  const hasIndexSql = indexSqlCount > 0;
  const readiness = targetReadiness(run, profile);
  const selectionOnlyReason = run.selectionOnly ? `Start a run for ${selectedOriginalDatabase(run) || "the selected source database"} first.` : "";

  actions.push(actionButton(
    run,
    "create-schema",
    `Create ${targetName} structure`,
    "primary-approval",
    selectionOnlyReason || (readiness.structureReady ? `${targetName} structure already exists.` : hasCreateSql && ["planned", "failed"].includes(createStep.status || "") ? "" : `Needs generated DDL for ${targetName} first.`),
  ));
  if (createStep.status === "failed" && hasCreateSql) {
    actions.push(actionButton(run, "repair-schema", "Repair schema SQL"));
  }
  actions.push(actionButton(
    run,
    "migrate-data",
    `Populate ${targetName}`,
    "primary-approval",
    selectionOnlyReason || (readiness.dataReady ? `${targetName} already appears populated.` : !readiness.structureReady ? `Create ${targetName} structure first.` : hasMigrationSql && ["planned", "failed"].includes(migrateStep.status || "") ? "" : "No migration SQL is available."),
  ));
  actions.push(actionButton(
    run,
    "regenerate-rewrite",
    `Generate ${targetName} workload`,
    "",
    selectionOnlyReason || (normalizationStep.status === "completed" && workloadStep.status === "completed" && ["planned", "completed", "failed"].includes(rewriteStep.status || "") ? "" : "Needs normalization and source workload first."),
  ));
  actions.push(actionButton(
    run,
    "create-secondary-indexes",
    `Create secondary indexes${indexSqlCount ? ` (${indexSqlCount})` : ""}`,
    "primary-approval",
    selectionOnlyReason || (!readiness.structureReady ? `Create ${targetName} structure first.` : hasIndexSql && indexCreateStep.status !== "completed" ? "" : indexCreateStep.status === "completed" ? "Secondary indexes are already created or not needed." : indexRecommendStep.status === "completed" ? "No secondary-index SQL is available." : "Run or regenerate index recommendations first."),
  ));
  actions.push(actionButton(
    run,
    "run-pgbench-new",
    `Run pgbench on ${targetName}`,
    "primary-approval",
    selectionOnlyReason || (newBench.details?.workload_path && readiness.structureReady && readiness.dataReady && rewriteStep.status === "completed"
      ? ""
      : !readiness.structureReady ? `Needs ${targetName} structure first.` : !readiness.dataReady ? `Needs ${targetName} data before benchmarking.` : rewriteStep.status !== "completed" || !newBench.details?.workload_path ? `Needs generated ${targetName} workload first.` : `${targetName} benchmark is not ready.`),
  ));
  return actions;
}

function secondaryIndexSqlStatements(run) {
  const indexCreateStep = step(run, "create_secondary_indexes");
  const indexRecommendStep = step(run, "optimize_indexes");
  return [
    ...(indexCreateStep.details?.sql_statements || []),
    ...(indexRecommendStep.details?.sql_statements || []),
  ].filter((statement, index, statements) => statement && statements.indexOf(statement) === index);
}

function renderActionShelf(run) {
  const actions = [...originalActions(run), ...targetActions(run)];
  return actions.length ? `<div class="action-shelf">${actions.join("")}</div>` : `<div class="empty-inline">No gated action is ready.</div>`;
}

function renderStepCards(run, names) {
  return `
    <div class="step-card-grid">
      ${names.map((name) => {
        const item = effectiveStep(run, name);
        const details = item.details || {};
        const detailSummary = details.summary || details.status || item.error || "";
        return `
          <article class="step-card step-${escapeHtml(item.status || "pending")}">
            <div class="step-card-head">
              <strong>${escapeHtml(item.title || name)}</strong>
              ${pill((item.status || "pending").replaceAll("_", " "), item.status || "pending")}
            </div>
            ${detailSummary ? `<p>${escapeHtml(detailSummary)}</p>` : ""}
            ${item.error ? `<p class="error-text">${escapeHtml(item.error)}</p>` : ""}
          </article>
        `;
      }).join("")}
    </div>
  `;
}

function renderSetup(run, sourceProfile) {
  return `
    <section class="tab-panel">
      <div class="panel-grid two">
        <article class="workspace-panel">
          <h2>Readiness</h2>
          ${renderStepCards(run, ["llm_gateway", "inspect_source_schema", "extract_workload_logs"])}
        </article>
        <article class="workspace-panel">
          <h2>Abstract Source Profile</h2>
          ${renderProfileCards(sourceProfile)}
          ${renderRiskSignals(sourceProfile)}
        </article>
      </div>
    </section>
  `;
}

function renderProfile(run, sourceProfile, targetProfile) {
  const sourceName = selectedOriginalDatabase(run) || sourceProfile.database || "source database";
  const targetName = selectedTargetDatabase(run) || targetProfile.database || "target database";
  return `
    <section class="tab-panel">
      <div class="panel-grid two">
        <article class="workspace-panel">
          <h2>${escapeHtml(sourceName)} Profile</h2>
          ${renderProfileCards(sourceProfile)}
          ${renderRiskSignals(sourceProfile)}
          <h3>Clusters</h3>
          ${renderClusters(sourceProfile)}
        </article>
        <article class="workspace-panel">
          <h2>${escapeHtml(targetName)} Profile</h2>
          ${renderProfileCards(targetProfile)}
          ${renderRiskSignals(targetProfile)}
          <h3>Clusters</h3>
          ${renderClusters(targetProfile)}
        </article>
      </div>
    </section>
  `;
}

function renderNormalize(run) {
  const normalize = step(run, "propose_3nf_schema");
  const details = normalize.details || {};
  const targetTables = details.target_tables || [];
  const targetColumns = targetTables.reduce((total, table) => total + (table.columns || []).length, 0);
  return `
    <section class="tab-panel">
      <article class="workspace-panel">
        <h2>Normalization Plan</h2>
        <div class="metric-grid">
          ${metricCard("Status", details.status || normalize.status || "pending")}
          ${metricCard("Findings", formatNumber((details.table_findings || []).length))}
          ${metricCard("Dependencies", formatNumber((details.functional_dependencies || []).length))}
          ${metricCard("Target tables", formatNumber(targetTables.length))}
          ${metricCard("Target columns", formatNumber(targetColumns))}
          ${metricCard("DDL statements", formatNumber((details.sql_statements || []).length))}
        </div>
        <p class="summary-text">${escapeHtml(details.summary || "No normalization result yet.")}</p>
        ${renderStepCards(run, ["propose_3nf_schema"])}
      </article>
    </section>
  `;
}

function renderMigrate(run, targetProfile) {
  const targetActionHtml = targetActions(run, targetProfile);
  const targetName = selectedTargetDatabase(run) || "target database";
  return `
    <section class="tab-panel">
      <div class="panel-grid two">
        <article class="workspace-panel">
          <h2>Target Database</h2>
          ${renderStepCards(run, ["create_target_schema", "migrate_data"])}
        </article>
        <article class="workspace-panel">
          <h2>${escapeHtml(targetName)} Actions</h2>
          ${targetActionHtml.length ? `<div class="action-shelf">${targetActionHtml.join("")}</div>` : `<div class="empty-inline">No target-database action is ready.</div>`}
        </article>
      </div>
    </section>
  `;
}

function renderWorkload(run, artifacts) {
  const logs = artifacts.extract_workload_logs || {};
  const rewrite = step(run, "rewrite_queries");
  const summaryTables = step(run, "suggest_summary_tables");
  const candidates = summaryTables.details?.candidates || [];
  return `
    <section class="tab-panel">
      <div class="panel-grid two">
        <article class="workspace-panel">
          <h2>Workload</h2>
          <div class="metric-grid">
            ${metricCard("Source", logs.source_kind || "pending")}
            ${metricCard("Statements", formatNumber(logs.statements_detected || 0))}
            ${metricCard("Transactions", formatNumber(logs.transactions_detected || 0))}
            ${metricCard("Rewrite statements", formatNumber(rewrite.details?.statement_count || (rewrite.details?.statements || []).length || 0))}
          </div>
          ${renderStepCards(run, ["extract_workload_logs", "rewrite_queries"])}
        </article>
        <article class="workspace-panel">
          <h2>Summary Tables</h2>
          <div class="metric-grid">
            ${metricCard("Candidates", formatNumber(candidates.length))}
            ${metricCard("Creation", summaryTables.details?.creation_status || "placeholder")}
          </div>
          ${candidates.length ? `
            <div class="candidate-list">
              ${candidates.map((candidate) => `
                <article class="candidate-card">
                  <strong>${escapeHtml(candidate.label)}</strong>
                  <span>${escapeHtml(candidate.pattern)}</span>
                  <p>${escapeHtml(candidate.reason)}</p>
                </article>
              `).join("")}
            </div>
          ` : `<div class="empty-inline">No summary-table candidates yet.</div>`}
        </article>
      </div>
    </section>
  `;
}

function renderPhysical(run) {
  const indexes = step(run, "optimize_indexes");
  const indexCreation = step(run, "create_secondary_indexes");
  const tuning = step(run, "tune_postgresql_conf");
  const indexSql = secondaryIndexSqlStatements(run);
  return `
    <section class="tab-panel">
      <div class="panel-grid two">
        <article class="workspace-panel">
          <h2>Secondary Indexes</h2>
          <div class="metric-grid">
            ${metricCard("Recommendations", formatNumber(indexes.details?.recommendation_count || 0))}
            ${metricCard("Create status", indexCreation.status || "pending")}
            ${metricCard("SQL statements", formatNumber(indexSql.length))}
          </div>
          <div class="action-shelf">${targetActions(run).filter((html) => html.includes("create-secondary-indexes")).join("")}</div>
          ${renderStepCards(run, ["optimize_indexes", "create_secondary_indexes"])}
        </article>
        <article class="workspace-panel">
          <h2>PostgreSQL Tuning</h2>
          <div class="metric-grid">
            ${metricCard("Recommendations", formatNumber(tuning.details?.recommendation_count || 0))}
            ${metricCard("Source", tuning.details?.source || "pending")}
          </div>
          ${renderStepCards(run, ["tune_postgresql_conf"])}
        </article>
      </div>
    </section>
  `;
}

function renderBenchmark(run) {
  const original = step(run, "run_pgbench_original");
  const target = step(run, "run_pgbench_new");
  const sourceName = selectedOriginalDatabase(run) || "source database";
  const targetName = selectedTargetDatabase(run) || "target database";
  return `
    <section class="tab-panel">
      <div class="panel-grid two">
        <article class="workspace-panel">
          <h2>${escapeHtml(sourceName)}</h2>
          <div class="metric-grid">
            ${metricCard("TPS", original.details?.throughput_tps ?? "pending")}
            ${metricCard("Latency", original.details?.average_latency_ms ? `${original.details.average_latency_ms} ms` : "pending")}
            ${metricCard("Energy", original.details?.joules_per_transaction ? `${original.details.joules_per_transaction} J/tx` : "pending")}
          </div>
          ${renderStepCards(run, ["run_pgbench_original"])}
        </article>
        <article class="workspace-panel">
          <h2>${escapeHtml(targetName)}</h2>
          <div class="metric-grid">
            ${metricCard("TPS", target.details?.throughput_tps ?? "pending")}
            ${metricCard("Latency", target.details?.average_latency_ms ? `${target.details.average_latency_ms} ms` : "pending")}
            ${metricCard("Energy", target.details?.joules_per_transaction ? `${target.details.joules_per_transaction} J/tx` : "pending")}
          </div>
          ${renderStepCards(run, ["run_pgbench_new"])}
        </article>
      </div>
    </section>
  `;
}

function renderCompare(run, artifacts) {
  const comparison = artifacts.compare_disk_usage || step(run, "compare_disk_usage").details || {};
  return `
    <section class="tab-panel">
      <article class="workspace-panel">
        <h2>Comparison</h2>
        <div class="metric-grid">
          ${metricCard("Status", comparison.status || "pending")}
          ${metricCard("Throughput change", comparison.throughput_change_percent !== undefined ? `${comparison.throughput_change_percent}%` : "pending")}
          ${metricCard("Latency improvement", comparison.latency_improvement_percent !== undefined ? `${comparison.latency_improvement_percent}%` : "pending")}
          ${metricCard("Storage change", comparison.size_change_percent !== undefined ? `${comparison.size_change_percent}%` : "pending")}
          ${metricCard("Energy change", comparison.energy_per_transaction_change_percent !== undefined ? `${comparison.energy_per_transaction_change_percent}%` : "pending")}
        </div>
        ${renderStepCards(run, ["compare_disk_usage", "collect_extended_metrics"])}
      </article>
    </section>
  `;
}

function renderActiveTab(run, artifacts, sourceProfile, targetProfile) {
  if (activeTab === "profile") return renderProfile(run, sourceProfile, targetProfile);
  if (activeTab === "normalize") return renderNormalize(run);
  if (activeTab === "migrate") return renderMigrate(run, targetProfile);
  if (activeTab === "workload") return renderWorkload(run, artifacts);
  if (activeTab === "physical") return renderPhysical(run);
  if (activeTab === "benchmark") return renderBenchmark(run);
  if (activeTab === "compare") return renderCompare(run, artifacts);
  return renderSetup(run, sourceProfile);
}

function renderPostgresStatus(payload) {
  const checks = (payload.checks || []).map((check) => `
    <article class="connection-check connection-${escapeHtml(check.status)}">
      <div class="connection-check-head">
        <strong>${escapeHtml(check.label || check.database)}</strong>
        <span>${escapeHtml(check.status)}</span>
      </div>
      ${check.error ? `<p class="connection-error">${escapeHtml(check.error)}</p>` : `<p>${escapeHtml(check.current_database || check.database)} ${escapeHtml(check.server_version || "")}</p>`}
    </article>
  `).join("");
  postgresStatusBox.hidden = false;
  postgresStatusBox.dataset.status = payload.status || "unknown";
  postgresStatusBox.innerHTML = `
    <div class="connection-head">
      <strong>PostgreSQL</strong>
      <span>${escapeHtml(payload.status || "unknown")}</span>
    </div>
    <div class="connection-target">
      <code>${escapeHtml(payload.user || "")}@${escapeHtml(payload.host || "")}:${escapeHtml(payload.port || "")}</code>
      <code>source ${escapeHtml(payload.original_database || "")} -> target ${escapeHtml(payload.new_database || "")}</code>
    </div>
    ${payload.hint ? `<div class="connection-hint">${escapeHtml(payload.hint)}</div>` : ""}
    <div class="connection-checks">${checks}</div>
  `;
}

function renderDatabaseOptions(payload) {
  discoveredDatabases = payload.databases || [];
  const configuredSource = payload.original_database || originalDatabaseSelect.value.trim();
  const currentSelection = originalDatabaseSelect.value.trim() || configuredSource;
  const names = new Set(discoveredDatabases.map((database) => database.name));
  if (configuredSource) names.add(configuredSource);
  if (currentSelection) names.add(currentSelection);

  originalDatabaseSelect.innerHTML = [...names].sort().map((name) => {
    const database = discoveredDatabases.find((item) => item.name === name) || {};
    const tableLabel = database.table_count === null || database.table_count === undefined
      ? ""
      : ` (${formatNumber(database.table_count)} tables)`;
    const disabled = database.can_connect === false ? " disabled" : "";
    return `<option value="${escapeHtml(name)}"${name === currentSelection ? " selected" : ""}${disabled}>${escapeHtml(name + tableLabel)}</option>`;
  }).join("");

  if (!originalDatabaseSelect.value && configuredSource) {
    originalDatabaseSelect.value = configuredSource;
  }
  if (!newDatabaseInput.value.trim() && payload.new_database) {
    newDatabaseInput.value = payload.new_database;
    lastAutoTargetDatabase = payload.new_database;
  }
  if (!schemasInput.value.trim() && (payload.schemas || []).length) {
    schemasInput.value = payload.schemas.join(", ");
  }
  updateDatabaseSelectionStatus();
}

async function discoverDatabases() {
  setStatus("Discovering PostgreSQL databases...");
  const data = await api("/api/postgres/discover", {
    method: "POST",
    body: JSON.stringify(requestContext()),
  });
  renderDatabaseOptions(data);
  setStatus(`Discovered ${formatNumber((data.databases || []).length)} database(s).`);
}

async function renderRuns(runs) {
  const latestRun = runs[0] || null;
  const currentRun = runMatchesSelection(latestRun) ? latestRun : selectionOnlyRun(latestRun);
  const artifacts = latestRun && runMatchesSelection(latestRun) ? await loadCurrentArtifacts(latestRun) : {};
  const sourceProfile = latestProfiles?.original
    || (runMatchesSelection(latestRun) && artifacts.inspect_source_schema ? profileFromSchema(artifacts.inspect_source_schema) : null)
    || pendingProfile(selectedOriginalDatabase(currentRun), "source");
  const targetProfile = latestProfiles?.target
    || (runMatchesSelection(latestRun) ? targetProfileFromRun(currentRun) : null)
    || pendingProfile(selectedTargetDatabase(currentRun), "target");
  const headerLabel = currentRun.selectionOnly ? "Selected database pair" : "Current run";
  const headerTitle = currentRun.selectionOnly
    ? `${selectedOriginalDatabase(currentRun) || "source"} -> ${selectedTargetDatabase(currentRun) || "target"}`
    : currentRun.run_id;
  const headerMeta = currentRun.selectionOnly
    ? (latestRun ? `Latest saved run ${latestRun.run_id} uses a different database pair.` : "No run exists for this database pair yet.")
    : currentRun.created_at;

  runsContainer.innerHTML = `
    <article class="workspace">
      <header class="workspace-head">
        <div>
          <span class="label">${escapeHtml(headerLabel)}</span>
          <strong>${escapeHtml(headerTitle)}</strong>
          <small>${escapeHtml(headerMeta)}</small>
          <small>Source database: ${escapeHtml(selectedOriginalDatabase(currentRun) || "not selected")}</small>
        </div>
        ${pill((currentRun.status || "pending").replaceAll("_", " "), currentRun.status || "pending")}
      </header>
      ${renderDatabasePair(currentRun, sourceProfile, targetProfile)}
      ${renderTabs(currentRun)}
      ${renderActiveTab(currentRun, artifacts, sourceProfile, targetProfile)}
    </article>
  `;
}

async function refreshRuns() {
  const data = await api("/api/runs");
  const runs = data.runs || [];
  const contextKey = requestContextKey();
  if (!latestProfiles || latestProfilesContext !== contextKey) {
    profileLoading = true;
    profileError = "";
    await renderRuns(runs);
    try {
      const profileData = await api("/api/postgres/profile", {
        method: "POST",
        body: JSON.stringify(requestContext()),
      });
      latestProfiles = profileData.profiles;
      latestProfilesContext = contextKey;
    } catch (error) {
      latestProfiles = null;
      latestProfilesContext = "";
      profileError = error.message;
    } finally {
      profileLoading = false;
    }
  }
  await renderRuns(runs);
  return runs;
}

async function pollRun(runId) {
  if (activePoll) {
    clearInterval(activePoll);
    activePoll = null;
  }

  let attempts = 0;
  const pollOnce = async () => {
    attempts += 1;
    const run = await api(`/api/runs/${runId}`);
    await refreshRuns();
    if (run.status === "running") {
      setStatus(`Run ${runId} is running.`);
      if (attempts >= 120) {
        clearInterval(activePoll);
        activePoll = null;
        setStatus(`Run ${runId} is still running after several minutes.`, true);
      }
      return;
    }
    clearInterval(activePoll);
    activePoll = null;
    setStatus(run.status === "completed" ? `Run ${runId} completed.` : `Run ${runId} needs attention.`, run.status !== "completed");
  };

  await pollOnce();
  activePoll = setInterval(() => {
    pollOnce().catch((error) => {
      clearInterval(activePoll);
      activePoll = null;
      setStatus(error.message, true);
    });
  }, 2000);
}

async function startRun() {
  setStatus("Starting collection run...");
  const data = await api("/api/runs", {
    method: "POST",
    body: JSON.stringify(requestContext()),
  });
  setStatus(`Run ${data.run_id} accepted.`);
  await pollRun(data.run_id);
}

async function triggerRunAction(runId, action) {
  const sourceName = selectedOriginalDatabase() || "source database";
  const targetName = selectedTargetDatabase() || "target database";
  const labels = {
    "create-schema": `Creating ${targetName} schema...`,
    "repair-schema": "Repairing schema SQL...",
    "reset-target-db": `Resetting ${targetName}...`,
    "migrate-data": "Migrating data...",
    "regenerate-rewrite": "Regenerating workload...",
    "create-secondary-indexes": "Creating secondary indexes...",
    "run-pgbench-original": `Benchmarking ${sourceName}...`,
    "run-pgbench-new": `Benchmarking ${targetName}...`,
  };
  setStatus(labels[action] || "Starting action...");
  const data = await api(`/api/runs/${runId}/actions/${action}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  setStatus(`Action ${data.action} accepted.`);
  await pollRun(data.run_id);
}

async function testLLM() {
  const config_path = configPathInput.value.trim();
  setStatus("Testing LLM gateway...");
  try {
    const data = await api("/api/llm/test", {
      method: "POST",
      body: JSON.stringify({ config_path }),
    });
    latestLlmStatus = {
      status: "completed",
      summary: "LLM gateway responded successfully.",
    };
    await refreshRuns();
    setStatus(JSON.stringify(data, null, 2));
  } catch (error) {
    latestLlmStatus = {
      status: "failed",
      summary: error.message,
    };
    await refreshRuns();
    throw error;
  }
}

async function checkPostgres() {
  setStatus("Checking PostgreSQL...");
  const data = await api("/api/postgres/status", {
    method: "POST",
    body: JSON.stringify(requestContext()),
  });
  latestPostgresStatus = data;
  updateDatabaseSelectionStatus();
  renderPostgresStatus(data);
  try {
    const profileData = await api("/api/postgres/profile", {
      method: "POST",
      body: JSON.stringify(requestContext()),
    });
    latestProfiles = profileData.profiles;
    await refreshRuns();
  } catch (error) {
    latestProfiles = null;
  }
  setStatus(data.status === "ok" ? "PostgreSQL is ready." : data.hint || "PostgreSQL check needs attention.", data.status !== "ok");
}

document.getElementById("runButton").addEventListener("click", () => {
  startRun().catch((error) => setStatus(error.message, true));
});

document.getElementById("discoverButton").addEventListener("click", () => {
  discoverDatabases().catch((error) => setStatus(error.message, true));
});

document.getElementById("llmButton").addEventListener("click", () => {
  testLLM().catch((error) => setStatus(error.message, true));
});

document.getElementById("postgresButton").addEventListener("click", () => {
  checkPostgres().catch((error) => setStatus(error.message, true));
});

document.getElementById("refreshButton").addEventListener("click", () => {
  refreshRuns().catch((error) => setStatus(error.message, true));
});

originalDatabaseSelect.addEventListener("change", () => {
  const source = originalDatabaseSelect.value.trim();
  if (!newDatabaseInput.value.trim() || newDatabaseInput.value.trim() === lastAutoTargetDatabase) {
    newDatabaseInput.value = source ? `${source}_new` : "";
    lastAutoTargetDatabase = newDatabaseInput.value.trim();
  }
  latestProfiles = null;
  latestProfilesContext = "";
  latestPostgresStatus = null;
  updateDatabaseSelectionStatus();
  refreshRuns().catch((error) => setStatus(error.message, true));
});

newDatabaseInput.addEventListener("input", () => {
  latestProfiles = null;
  latestProfilesContext = "";
  updateDatabaseSelectionStatus();
  refreshRuns().catch((error) => setStatus(error.message, true));
});

schemasInput.addEventListener("input", () => {
  latestProfiles = null;
  latestProfilesContext = "";
  updateDatabaseSelectionStatus();
  refreshRuns().catch((error) => setStatus(error.message, true));
});

configPathInput.addEventListener("input", () => {
  latestProfiles = null;
  latestProfilesContext = "";
  latestPostgresStatus = null;
});

workloadPathInput.addEventListener("input", () => {
  latestProfiles = null;
  latestProfilesContext = "";
});

runsContainer.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    activeTab = tab.dataset.tab;
    refreshRuns().catch((error) => setStatus(error.message, true));
    return;
  }

  const button = event.target.closest("[data-action]");
  if (!button) return;
  triggerRunAction(button.dataset.runId, button.dataset.action).catch((error) => setStatus(error.message, true));
});

updateDatabaseSelectionStatus();
refreshRuns().catch((error) => setStatus(error.message, true));
