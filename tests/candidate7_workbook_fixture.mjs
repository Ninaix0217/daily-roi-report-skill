import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

async function loadArtifactTool() {
  const nodeModules = process.env.DAILY_ROI_NODE_MODULES;
  if (!nodeModules) return import("@oai/artifact-tool");
  const req = createRequire(import.meta.url);
  const resolved = req.resolve("@oai/artifact-tool", { paths: [nodeModules] });
  return import(pathToFileURL(resolved).href);
}

const { SpreadsheetFile, Workbook } = await loadArtifactTool();

const [outputDir, sourceBrushText = "missing"] = process.argv.slice(2);
if (!outputDir) throw new Error("output directory is required");

const targetDate = "2026-08-18";
const product = "PRODUCT_A";
const store = "STORE_A";
const sku = "100001";

async function save(workbook, fileName) {
  await (await SpreadsheetFile.exportXlsx(workbook)).save(path.join(outputDir, fileName));
}

await fs.mkdir(outputDir, { recursive: true });

const template = Workbook.create();
const report = template.worksheets.add("REPORT");
report.getRange("A1").values = [[targetDate]];
report.getRange("A3:E5").values = [
  ["产品", null, "付费总费用", "真实销售额", "综合投产比"],
  [product, null, null, null, null],
  ["合计", null, null, null, null],
];
const skuSheet = template.worksheets.add("SKU");
skuSheet.getRange("A1:H2").values = [
  ["商品名称", "单盒SKU", "三盒SKU", null, null, "销售总计", "刷单金额", "真实销售额"],
  [product, sku, null, null, null, null, sourceBrushText === "missing" ? null : Number(sourceBrushText), null],
];
skuSheet.getRange("H2").formulas = [["=ROUND(F2-G2,2)"]];
const stores = template.worksheets.add("STORES");
stores.getRange("A1:B1").values = [[store, product]];
await save(template, "template.xlsx");

const finance = Workbook.create();
const financeSheet = finance.worksheets.add("FINANCE");
financeSheet.getRange("A1:E2").values = [
  ["流水单号", "投放账户名称", "投放日期", "交易类型", "支出"],
  ["TX-C7-SYNTHETIC", store, targetDate, "快车扣费", 0],
];
await save(finance, "finance.xlsx");

const sales = Workbook.create();
const salesSheet = sales.worksheets.add("SALES");
salesSheet.getRange("A1:C3").values = [
  ["日期", "SKU", "成交金额"],
  [targetDate, "合计", 100],
  [targetDate, sku, 100],
];
await save(sales, "sales.xlsx");
