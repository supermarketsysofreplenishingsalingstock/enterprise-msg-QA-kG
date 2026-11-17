var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// server/utils/run_archive.ts
var run_archive_exports = {};
__export(run_archive_exports, {
  archiveRunResult: () => archiveRunResult,
  listRunArchive: () => listRunArchive,
  makeRunId: () => makeRunId,
  readRunArchive: () => readRunArchive,
  summarizeRun: () => summarizeRun
});
module.exports = __toCommonJS(run_archive_exports);
var import_node_fs = require("node:fs");
var import_node_path = require("node:path");
var MAX_ARCHIVED_RUNS = 50;
function runsDir() {
  const dir = (0, import_node_path.join)(process.cwd(), ".data", "runs");
  if (!(0, import_node_fs.existsSync)(dir)) (0, import_node_fs.mkdirSync)(dir, { recursive: true });
  return dir;
}
function indexPath() {
  return (0, import_node_path.join)(runsDir(), "index.json");
}
function safeId(id) {
  return String(id ?? "").trim().replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 80);
}
function readIndex() {
  try {
    const raw = (0, import_node_fs.readFileSync)(indexPath(), "utf8");
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}
function writeIndex(list) {
  try {
    (0, import_node_fs.writeFileSync)(indexPath(), JSON.stringify(list, null, 2), "utf8");
  } catch {
  }
}
function countOf(v) {
  return Array.isArray(v) ? v.length : 0;
}
function firstHostOf(result) {
  try {
    const seeds = result?.taskPlan?.seeds ?? result?.plan?.seeds;
    if (Array.isArray(seeds) && seeds.length) {
      const first = typeof seeds[0] === "string" ? seeds[0] : seeds[0]?.url;
      const m = /https?:\/\/([^/\s]+)/i.exec(String(first ?? ""));
      if (m) return m[1];
    }
  } catch {
  }
  return "";
}
function seedCountOf(result) {
  try {
    const seeds = result?.taskPlan?.seeds ?? result?.plan?.seeds;
    if (Array.isArray(seeds)) return seeds.length;
  } catch {
  }
  return 0;
}
function makeRunId(startedAt) {
  const ts = Date.now();
  const rand = Math.random().toString(36).slice(2, 8);
  const stamp = String(startedAt ?? "").replace(/[^0-9]/g, "").slice(0, 14);
  return `run_${stamp || ts}_${rand}`;
}
function summarizeRun(id, result) {
  return {
    id,
    task: String(result?.task ?? "").slice(0, 160),
    startedAt: String(result?.startedAt ?? ""),
    finishedAt: String(result?.finishedAt ?? ""),
    status: String(result?.status ?? ""),
    attempts: Number(result?.attempts ?? 0) || void 0,
    pages: countOf(result?.kbPages),
    items: countOf(result?.items),
    attachments: countOf(result?.attachments),
    images: countOf(result?.images),
    kbPages: countOf(result?.kbPages),
    seedHost: firstHostOf(result) || void 0,
    seedCount: seedCountOf(result) || void 0
  };
}
async function archiveRunResult(result, explicitId) {
  try {
    if (!result || typeof result !== "object") return null;
    const id = safeId(explicitId ?? makeRunId(result?.startedAt));
    if (!id) return null;
    (0, import_node_fs.writeFileSync)((0, import_node_path.join)(runsDir(), `${id}.json`), JSON.stringify({ ...result, runId: id }, null, 2), "utf8");
    const list = readIndex().filter((e) => e && e.id !== id);
    list.unshift(summarizeRun(id, result));
    while (list.length > MAX_ARCHIVED_RUNS) {
      const dropped = list.pop();
      if (dropped?.id) {
        try {
          (0, import_node_fs.unlinkSync)((0, import_node_path.join)(runsDir(), `${dropped.id}.json`));
        } catch {
        }
      }
    }
    writeIndex(list);
    return list[0] ?? null;
  } catch {
    return null;
  }
}
function listRunArchive() {
  let list = readIndex();
  if (!list.length) {
    try {
      const dir = runsDir();
      const files = (0, import_node_fs.readdirSync)(dir).filter((f) => /^run_[a-zA-Z0-9_-]+\.json$/.test(f) && f !== "index.json").map((f) => ({ f, m: (0, import_node_fs.statSync)((0, import_node_path.join)(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m).slice(0, MAX_ARCHIVED_RUNS);
      for (const { f } of files) {
        try {
          const data = JSON.parse((0, import_node_fs.readFileSync)((0, import_node_path.join)(dir, f), "utf8"));
          list.push(summarizeRun(f.replace(/\.json$/, ""), data));
        } catch {
        }
      }
    } catch {
    }
  }
  return list;
}
function readRunArchive(id) {
  const key = safeId(id);
  if (!key || key === "index") return null;
  try {
    return JSON.parse((0, import_node_fs.readFileSync)((0, import_node_path.join)(runsDir(), `${key}.json`), "utf8"));
  } catch {
    return null;
  }
}
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  archiveRunResult,
  listRunArchive,
  makeRunId,
  readRunArchive,
  summarizeRun
});
