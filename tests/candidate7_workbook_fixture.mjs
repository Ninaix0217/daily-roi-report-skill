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

const [outputDir, sourceBrushText = "missing", productCountText = "1"] = process.argv.slice(2);
if (!outputDir) throw new Error("output directory is required");

const targetDate = "2026-08-18";
const store = "STORE_A";
const productCount = Number.parseInt(productCountText, 10);
if (!Number.isInteger(productCount) || productCount < 1 || productCount > 2) {
  throw new Error("product count must be 1 or 2");
}
const products = [
  { name: "PRODUCT_A", sku: "100001" },
  { name: "PRODUCT_B", sku: "100002" },
].slice(0, productCount);

async function save(workbook, fileName) {
  await (await SpreadsheetFile.exportXlsx(workbook)).save(path.join(outputDir, fileName));
}

await fs.mkdir(outputDir, { recursive: true });

const template = Workbook.create();
const report = template.worksheets.add("REPORT");
report.getRange("A1").values = [[targetDate]];
report.getRange(`A3:E${4 + products.length}`).values = [
  ["产品", null, "付费总费用", "真实销售额", "综合投产比"],
  ...products.map(({ name }) => [name, null, null, null, null]),
  ["合计", null, null, null, null],
];
const skuSheet = template.worksheets.add("SKU");
skuSheet.getRange(`A1:H${1 + products.length}`).values = [
  ["商品名称", "单盒SKU", "三盒SKU", null, null, "销售总计", "刷单金额", "真实销售额"],
  ...products.map(({ name, sku }, index) => [
    name,
    sku,
    null,
    null,
    null,
    null,
    index === 0 && sourceBrushText !== "missing" ? Number(sourceBrushText) : null,
    null,
  ]),
];
for (let row = 2; row < 2 + products.length; row += 1) {
  skuSheet.getRange(`H${row}`).formulas = [[`=ROUND(F${row}-G${row},2)`]];
}
const stores = template.worksheets.add("STORES");
stores.getRange(`A1:B${products.length}`).values = products.map(({ name }, index) => [index === 0 ? store : null, name]);
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
const perProductSales = 100 / products.length;
salesSheet.getRange(`A1:C${2 + products.length}`).values = [
  ["日期", "SKU", "成交金额"],
  [targetDate, "合计", 100],
  ...products.map(({ sku }) => [targetDate, sku, perProductSales]),
];
await save(sales, "sales.xlsx");
