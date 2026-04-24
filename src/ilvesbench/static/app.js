const statusBox = document.getElementById("statusBox");
const runsContainer = document.getElementById("runs");
const configPathInput = document.getElementById("configPath");
const workloadPathInput = document.getElementById("workloadPath");
const postgresStatusBox = document.getElementById("postgresStatus");
let activePoll = null;
let openPanels = new Set();

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

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderValue(value) {
  if (Array.isArray(value)) {
    return value.join(", ");
  }
  if (value && typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function panelKey(runId, name) {
  return `${runId}:${name}`;
}

function isPanelOpen(runId, name) {
  return openPanels.has(panelKey(runId, name));
}

function renderCollapsible(runId, name, title, meta, contentRenderer) {
  const open = isPanelOpen(runId, name);
  return `
    <section class="collapsible" data-panel="${escapeHtml(name)}">
      <button class="collapsible-toggle" type="button" data-toggle-panel="${escapeHtml(name)}" data-run-id="${escapeHtml(runId)}" aria-expanded="${open ? "true" : "false"}">
        <span class="collapsible-title">${escapeHtml(title)}</span>
        <span class="collapsible-meta">${escapeHtml(meta)}</span>
        <span class="collapsible-caret">${open ? "Hide" : "Show"}</span>
      </button>
      ${open ? `<div class="collapsible-body">${contentRenderer()}</div>` : ""}
    </section>
  `;
}

function compactList(items, max = 250) {
  if (!Array.isArray(items)) {
    return [];
  }
  return items.slice(0, max);
}

function renderPostgresStatus(payload) {
  const checks = (payload.checks || [])
    .map((check) => {
      const details = check.status === "ok"
        ? `
          <div class="connection-detail">Connected as <code>${escapeHtml(check.current_user || "")}</code> to <code>${escapeHtml(check.current_database || check.database)}</code></div>
          <div class="connection-detail">Server <code>${escapeHtml(check.server_version || "")}</code>${check.can_create_database === false ? " · no CREATEDB privilege detected" : ""}</div>
        `
        : `
          <div class="connection-error">${escapeHtml(check.error || "Connection check failed.")}</div>
        `;
      const hint = check.hint ? `<div class="connection-hint">${escapeHtml(check.hint)}</div>` : "";
      return `
        <article class="connection-check connection-${escapeHtml(check.status)}">
          <div class="connection-check-head">
            <strong>${escapeHtml(check.label || check.database)}</strong>
            <span>${escapeHtml(check.status)}</span>
          </div>
          ${details}
          ${hint}
        </article>
      `;
    })
    .join("");
  postgresStatusBox.hidden = false;
  postgresStatusBox.dataset.status = payload.status || "unknown";
  postgresStatusBox.innerHTML = `
    <div class="connection-head">
      <strong>PostgreSQL connection</strong>
      <span>${escapeHtml(payload.status || "unknown")}</span>
    </div>
    <div class="connection-target">
      <code>${escapeHtml(payload.user || "")}@${escapeHtml(payload.host || "")}:${escapeHtml(payload.port || "")}</code>
      <code>${escapeHtml(payload.original_database || "")} -> ${escapeHtml(payload.new_database || "")}</code>
    </div>
    ${payload.hint ? `<div class="connection-hint">${escapeHtml(payload.hint)}</div>` : ""}
    <div class="connection-checks">${checks}</div>
  `;
}

function renderRunActions(run) {
  const normalizationStep = (run.steps || []).find((step) => step.name === "propose_3nf_schema");
  const createStep = (run.steps || []).find((step) => step.name === "create_target_schema");
  const migrateStep = (run.steps || []).find((step) => step.name === "migrate_data");
  const rewriteStep = (run.steps || []).find((step) => step.name === "rewrite_queries");
  const pgbenchOriginalStep = (run.steps || []).find((step) => step.name === "run_pgbench_original");
  const pgbenchNewStep = (run.steps || []).find((step) => step.name === "run_pgbench_new");
  const schemaSql = createStep?.details?.sql_statements || normalizationStep?.details?.sql_statements || [];
  const canResetTargetDb =
    schemaSql.length &&
    ["planned", "completed", "failed"].includes(createStep?.status || "");
  const canRunOriginalBenchmark =
    !!pgbenchOriginalStep?.details?.workload_path &&
    ["planned", "completed", "failed"].includes(pgbenchOriginalStep?.status || "");
  const canRunNewBenchmark =
    !!pgbenchNewStep?.details?.workload_path &&
    ["planned", "completed", "failed"].includes(pgbenchNewStep?.status || "") &&
    createStep?.status === "completed" &&
    migrateStep?.status === "completed" &&
    rewriteStep?.status === "completed";
  const canRegenerateRewrite =
    normalizationStep?.status === "completed" &&
    (run.steps || []).find((step) => step.name === "extract_workload_logs")?.status === "completed" &&
    ["planned", "completed", "failed"].includes(rewriteStep?.status || "");
  const actions = [];

  if (createStep?.status === "planned" && (createStep.details?.sql_statements || []).length) {
    actions.push(`
      <button class="action-button primary-approval" data-action="create-schema" data-run-id="${escapeHtml(run.run_id)}">
        Approve db-new creation
      </button>
    `);
  }

  if (createStep?.status === "failed" && schemaSql.length) {
    actions.push(`
      <button class="action-button" data-action="repair-schema" data-run-id="${escapeHtml(run.run_id)}">
        Repair schema SQL with LLM
      </button>
    `);
  }

  if (canResetTargetDb) {
    actions.push(`
      <button class="action-button" data-action="reset-target-db" data-run-id="${escapeHtml(run.run_id)}">
        Drop db-new and reset
      </button>
    `);
  }

  if (
    createStep?.status === "completed" &&
    migrateStep?.status === "planned" &&
    (migrateStep.details?.statements || []).length
  ) {
    actions.push(`
      <button class="action-button primary-approval" data-action="migrate-data" data-run-id="${escapeHtml(run.run_id)}">
        Approve data migration
      </button>
    `);
  }

  if (canRunOriginalBenchmark) {
    actions.push(`
      <button class="action-button primary-approval" data-action="run-pgbench-original" data-run-id="${escapeHtml(run.run_id)}">
        Run pgbench on db-original
      </button>
    `);
  }

  if (canRegenerateRewrite) {
    actions.push(`
      <button class="action-button" data-action="regenerate-rewrite" data-run-id="${escapeHtml(run.run_id)}">
        Regenerate rewritten workload
      </button>
    `);
  }

  if (canRunNewBenchmark) {
    actions.push(`
      <button class="action-button primary-approval" data-action="run-pgbench-new" data-run-id="${escapeHtml(run.run_id)}">
        Run pgbench on db-new
      </button>
    `);
  }

  if (!actions.length) {
    return `
      <div class="run-actions-info">
        No approval action is available yet for this run.
      </div>
    `;
  }

  return `
    <div class="run-actions-panel">
      <p class="approval-heading">Human approval</p>
      <div class="run-actions">${actions.join("")}</div>
    </div>
  `;
}

function renderSummary(run) {
  const summaryEntries = Object.entries(run.summary || {});
  return summaryEntries.length
    ? renderCollapsible(run.run_id, "summary", "Run summary", `${summaryEntries.length} fields`, () => {
        const summary = summaryEntries
          .map(
            ([key, value]) => `
              <div class="detail-row">
                <span>${escapeHtml(key)}</span>
                <code>${escapeHtml(renderValue(value))}</code>
              </div>
            `
          )
          .join("");
        return `<div class="run-summary">${summary}</div>`;
      })
    : "";
}

function renderSteps(run) {
  const steps = (run.steps || [])
    .map((step) => {
      const approval = step.requires_approval
        ? '<p class="step-note">Human approval required before execution.</p>'
        : "";
      const error = step.error ? `<p class="step-error">${escapeHtml(step.error)}</p>` : "";
      const detailEntries = Object.entries(step.details || {});
      const detailCount = detailEntries.length;
      const stepKey = `step-${step.name}`;
      const detailsPanel = detailCount
        ? renderCollapsible(
            run.run_id,
            stepKey,
            "Step details",
            `${detailCount} fields`,
            () => {
              const details = detailEntries
                .map(
                  ([key, value]) => `
                    <div class="detail-row">
                      <span>${escapeHtml(key)}</span>
                      <code>${escapeHtml(renderValue(value))}</code>
                    </div>
                  `
                )
                .join("");
              return `<div class="step-details">${details}</div>`;
            },
          )
        : "";

      return `
        <li class="step step-${step.status}">
          <div class="step-head">
            <strong>${escapeHtml(step.title)}</strong>
            <span>${escapeHtml(step.status)}</span>
          </div>
          ${approval}
          ${error}
          ${detailsPanel}
        </li>
      `;
    })
    .join("");

  return renderCollapsible(run.run_id, "steps", "Pipeline steps", `${(run.steps || []).length} steps`, () => `<ul class="step-list">${steps}</ul>`);
}

function renderSourceTable(table) {
  const columns = (table.columns || [])
    .map(
      (column) => `
        <li>
          <code>${escapeHtml(column.name)}</code>
          <span>${escapeHtml(column.data_type)}</span>
        </li>
      `
    )
    .join("");
  return `
    <article class="preview-table">
      <h4>${escapeHtml(`${table.schema}.${table.name}`)}</h4>
      <ul>${columns}</ul>
    </article>
  `;
}

function renderTargetTable(table) {
  const columns = (table.columns || [])
    .map(
      (column) => `
        <li>
          <code>${escapeHtml(column.name)}</code>
          <span>${escapeHtml(`${column.source_table}.${column.source_column}`)}</span>
        </li>
      `
    )
    .join("");
  const keyInfo = [];
  if ((table.primary_key || []).length) {
    keyInfo.push(`PK: ${(table.primary_key || []).join(", ")}`);
  }
  if ((table.foreign_keys || []).length) {
    keyInfo.push(
      ...table.foreign_keys.map((foreignKey) => {
        return `FK: ${foreignKey.columns.join(", ")} -> ${foreignKey.references_table}(${foreignKey.references_columns.join(", ")})`;
      }),
    );
  }
  return `
    <article class="preview-table normalized-table">
      <h4>${escapeHtml(table.name)}</h4>
      <p class="preview-purpose">${escapeHtml(table.purpose || "")}</p>
      <ul>${columns}</ul>
      ${keyInfo.length ? `<div class="preview-notes">${keyInfo.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}</div>` : ""}
    </article>
  `;
}

function renderPreviewPanel(run, artifacts) {
  const normalizationStep = (run.steps || []).find((step) => step.name === "propose_3nf_schema");
  const migrationStep = (run.steps || []).find((step) => step.name === "migrate_data");
  const rewriteStep = (run.steps || []).find((step) => step.name === "rewrite_queries");
  const sourceTables = artifacts?.inspect_source_schema?.tables || [];
  const targetTables = normalizationStep?.details?.target_tables || [];
  const sqlStatements = normalizationStep?.details?.sql_statements || [];
  const migrationStatements = migrationStep?.details?.statements || [];
  const rewrittenStatements = rewriteStep?.details?.statements || [];
  const sourceColumnCount = sourceTables.reduce((total, table) => total + (table.columns || []).length, 0);
  const targetColumnCount = targetTables.reduce((total, table) => total + (table.columns || []).length, 0);
  const renderSourceHtml = () => {
    const tables = compactList(sourceTables);
    const overflow = sourceTables.length > tables.length
      ? `<div class="empty preview-empty">Showing ${tables.length} of ${sourceTables.length} source tables.</div>`
      : "";
    return tables.length
      ? tables.map((table) => renderSourceTable(table)).join("") + overflow
      : '<div class="empty preview-empty">Source schema preview unavailable.</div>';
  };
  const renderTargetHtml = () => {
    const tables = compactList(targetTables);
    const overflow = targetTables.length > tables.length
      ? `<div class="empty preview-empty">Showing ${tables.length} of ${targetTables.length} target tables.</div>`
      : "";
    return tables.length
      ? tables.map((table) => renderTargetTable(table)).join("") + overflow
      : '<div class="empty preview-empty">No normalized target tables proposed yet.</div>';
  };
  const renderMigrationHtml = () => {
    const statements = compactList(migrationStatements);
    const overflow = migrationStatements.length > statements.length
      ? `<div class="empty preview-empty">Showing ${statements.length} of ${migrationStatements.length} migration statements.</div>`
      : "";
    return statements.length
      ? statements
          .map(
            (statement) => `
              <article class="sql-card">
                <p class="sql-purpose">${escapeHtml(statement.purpose || statement.target_table)}</p>
                <pre>${escapeHtml(statement.sql)}</pre>
              </article>
            `,
          )
          .join("") + overflow
      : '<div class="empty preview-empty">No migration SQL proposed yet.</div>';
  };
  const renderDdlHtml = () => {
    const statements = compactList(sqlStatements);
    const overflow = sqlStatements.length > statements.length
      ? `<div class="empty preview-empty">Showing ${statements.length} of ${sqlStatements.length} DDL statements.</div>`
      : "";
    return statements.length
      ? statements
          .map(
            (statement) => `
              <article class="sql-card">
                <p class="sql-purpose">Target schema DDL</p>
                <pre>${escapeHtml(statement)}</pre>
              </article>
            `,
          )
          .join("") + overflow
      : '<div class="empty preview-empty">No target schema SQL proposed yet.</div>';
  };
  const renderRewrittenHtml = () => {
    const statements = compactList(rewrittenStatements);
    const overflow = rewrittenStatements.length > statements.length
      ? `<div class="empty preview-empty">Showing ${statements.length} of ${rewrittenStatements.length} rewritten queries.</div>`
      : "";
    return statements.length
      ? statements
          .map(
            (statement) => `
              <article class="sql-card">
                <p class="sql-purpose">Rewritten db-new workload query</p>
                <pre>${escapeHtml(statement)}</pre>
              </article>
            `,
          )
          .join("") + overflow
      : '<div class="empty preview-empty">No rewritten db-new workload is available yet.</div>';
  };

  return `
    <section class="preview-panel">
      <div class="section-header">
        <h3>Preview diff</h3>
        <p>${sourceTables.length} source tables, ${sourceColumnCount} source columns, ${targetTables.length} target tables, ${targetColumnCount} target columns.</p>
      </div>
      ${renderCollapsible(run.run_id, "preview-source", "Source tables", `${sourceTables.length} tables, ${sourceColumnCount} columns`, renderSourceHtml)}
      ${renderCollapsible(run.run_id, "preview-target", "Normalized target tables", `${targetTables.length} tables, ${targetColumnCount} columns`, renderTargetHtml)}
      ${renderCollapsible(run.run_id, "preview-ddl", "Planned target schema SQL", `${sqlStatements.length} statements`, renderDdlHtml)}
      ${renderCollapsible(run.run_id, "preview-migration", "Planned data migration SQL", `${migrationStatements.length} statements`, renderMigrationHtml)}
      ${renderCollapsible(run.run_id, "preview-rewrite", "Rewritten workload for db-new", `${rewrittenStatements.length} statements`, renderRewrittenHtml)}
    </section>
  `;
}

async function fetchArtifact(runId, artifactName) {
  return api(`/api/runs/${runId}/artifacts/${artifactName}`);
}

async function renderRuns(runs) {
  if (!runs.length) {
    runsContainer.innerHTML = '<div class="empty">No runs yet.</div>';
    return;
  }

  const currentRun = runs[0];

  let currentArtifacts = {};
  try {
    const artifactPromises = [];
    if (currentRun.artifacts?.inspect_source_schema) {
      artifactPromises.push(
        fetchArtifact(currentRun.run_id, "inspect_source_schema").then((payload) => {
          currentArtifacts.inspect_source_schema = payload;
        }),
      );
    }
    await Promise.all(artifactPromises);
  } catch (error) {
    setStatus(`Could not load preview artifacts: ${error.message}`, true);
  }

  const currentSection = `
    <section class="current-run-layout">
      <article class="run-card current-run-card">
        <p class="run-label">Current run</p>
        <div class="run-topline">
          <span class="run-id">${escapeHtml(currentRun.run_id)}</span>
          <span class="badge badge-${escapeHtml(currentRun.status)}">${escapeHtml(currentRun.status)}</span>
        </div>
        <p class="run-meta">${escapeHtml(currentRun.created_at)}</p>
        <p class="run-meta">${escapeHtml(currentRun.config_path)}</p>
        ${renderRunActions(currentRun)}
        ${renderPreviewPanel(currentRun, currentArtifacts)}
        ${renderSummary(currentRun)}
        ${renderSteps(currentRun)}
      </article>
    </section>
  `;
  runsContainer.innerHTML = currentSection;
}

async function refreshRuns() {
  const data = await api("/api/runs");
  await renderRuns(data.runs || []);
  return data.runs || [];
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
      setStatus(`Run ${runId} is still running. The current-run panel is updating automatically.`);
      if (attempts >= 120) {
        clearInterval(activePoll);
        activePoll = null;
        setStatus(
          `Run ${runId} is still marked running after several minutes. Check the current-run panel for failed-step details.`,
          true,
        );
      }
      return;
    }

    clearInterval(activePoll);
    activePoll = null;

    const normalizationStep = (run.steps || []).find((step) => step.name === "propose_3nf_schema");
    const normalizationSummary = normalizationStep?.details?.summary
      ? ` Normalization note: ${normalizationStep.details.summary}`
      : "";

    if (run.status === "completed") {
      const createStep = (run.steps || []).find((step) => step.name === "create_target_schema");
      const migrateStep = (run.steps || []).find((step) => step.name === "migrate_data");
      const rewriteStep = (run.steps || []).find((step) => step.name === "rewrite_queries");
      const pgbenchOriginalStep = (run.steps || []).find((step) => step.name === "run_pgbench_original");
      const pgbenchNewStep = (run.steps || []).find((step) => step.name === "run_pgbench_new");
      const pendingActions = [];
      if (createStep?.status === "planned" && (createStep.details?.sql_statements || []).length) {
        pendingActions.push("db-new schema creation");
      }
      if (migrateStep?.status === "planned" && (migrateStep.details?.statements || []).length) {
        pendingActions.push("data migration");
      }
      if (pgbenchOriginalStep?.status === "planned" && pgbenchOriginalStep?.details?.workload_path) {
        pendingActions.push("db-original benchmarking");
      }
      if (
        pgbenchNewStep?.status === "planned" &&
        pgbenchNewStep?.details?.workload_path &&
        createStep?.status === "completed" &&
        migrateStep?.status === "completed" &&
        rewriteStep?.status === "completed"
      ) {
        pendingActions.push("db-new benchmarking");
      }
      if (pendingActions.length) {
        setStatus(
          `Run ${runId} finished its planning phase. Use the approval buttons in the current run card for: ${pendingActions.join(", ")}.${normalizationSummary}`,
        );
        return;
      }
      setStatus(`Run ${runId} completed.${normalizationSummary}`);
      return;
    }

    if (run.status === "awaiting_input") {
      const workloadStep = (run.steps || []).find((step) => step.name === "extract_workload_logs");
      const recommendation = workloadStep?.details?.recommended_action
        ? ` ${workloadStep.details.recommended_action}`
        : "";
      const createStep = (run.steps || []).find((step) => step.name === "create_target_schema");
      if (createStep?.status === "failed") {
        setStatus(
          `Run ${runId} needs human review. PostgreSQL rejected the proposed db-new schema, and you can now repair the SQL with the LLM or reset db-new.${normalizationSummary}`,
          true,
        );
        return;
      }
      setStatus(`Run ${runId} is waiting for human input.${recommendation}${normalizationSummary}`, true);
      return;
    }

    setStatus(`Run ${runId} failed.${normalizationSummary}`, true);
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
  const config_path = configPathInput.value.trim();
  const workload_path = workloadPathInput.value.trim();
  setStatus("Starting MVP collection run...");
  const data = await api("/api/runs", {
    method: "POST",
    body: JSON.stringify({ config_path, workload_path }),
  });
  setStatus(`Run ${data.run_id} accepted. Tracking progress...`);
  await pollRun(data.run_id);
}

async function triggerRunAction(runId, action) {
  const labels = {
    "create-schema": "Starting db-new schema creation...",
    "repair-schema": "Asking the LLM to repair the failed schema SQL...",
    "reset-target-db": "Dropping db-new and resetting approval steps...",
    "migrate-data": "Starting data migration...",
    "regenerate-rewrite": "Regenerating the rewritten workload for db-new...",
    "run-pgbench-original": "Starting pgbench against db-original...",
    "run-pgbench-new": "Starting pgbench against db-new...",
  };
  setStatus(labels[action] || "Starting action...");
  const data = await api(`/api/runs/${runId}/actions/${action}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  setStatus(`Action ${data.action} accepted for ${data.run_id}. Tracking progress...`);
  await pollRun(data.run_id);
}

async function testLLM() {
  const config_path = configPathInput.value.trim();
  setStatus("Testing LLM gateway...");
  const data = await api("/api/llm/test", {
    method: "POST",
    body: JSON.stringify({ config_path }),
  });
  setStatus(JSON.stringify(data, null, 2));
}

async function checkPostgres() {
  const config_path = configPathInput.value.trim();
  const workload_path = workloadPathInput.value.trim();
  setStatus("Checking PostgreSQL connection...");
  const data = await api("/api/postgres/status", {
    method: "POST",
    body: JSON.stringify({ config_path, workload_path }),
  });
  renderPostgresStatus(data);
  if (data.status === "ok") {
    setStatus("PostgreSQL connection check completed successfully.");
    return;
  }
  if (data.status === "warning") {
    setStatus(data.hint || "PostgreSQL connection works, but a permission warning was detected.", true);
    return;
  }
  setStatus(data.hint || "PostgreSQL connection check failed.", true);
}

document.getElementById("runButton").addEventListener("click", () => {
  startRun().catch((error) => setStatus(error.message, true));
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

runsContainer.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-toggle-panel]");
  if (toggle) {
    const key = panelKey(toggle.dataset.runId, toggle.dataset.togglePanel);
    if (openPanels.has(key)) {
      openPanels.delete(key);
    } else {
      openPanels.add(key);
    }
    refreshRuns().catch((error) => setStatus(error.message, true));
    return;
  }

  const button = event.target.closest("[data-action]");
  if (!button) {
    return;
  }
  const runId = button.dataset.runId;
  const action = button.dataset.action;
  triggerRunAction(runId, action).catch((error) => setStatus(error.message, true));
});

refreshRuns().catch((error) => setStatus(error.message, true));
