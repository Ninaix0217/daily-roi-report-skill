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

const { FileBlob, SpreadsheetFile } = await loadArtifactTool();

function text(value) {
  return String(value ?? "").replace(/\u00a0/g, " ").replace(/\r?\n/g, " ").trim();
}

function key(value) {
  return text(value).toLowerCase().replace(/[\s_（）()\-—]+/g, "");
}

function identityKind(value) {
  return ({
    sku: "sku",
    skuid: "sku",
    商品id: "product_id",
    商品编号: "product_id",
    投放id: "placement_id",
    投放编号: "placement_id",
    投放商品id: "platform_item_id",
    投放商品编号: "platform_item_id",
    平台商品id: "platform_item_id",
    平台商品编号: "platform_item_id",
    平台稳定商品标识: "platform_item_id",
  })[key(value)] ?? null;
}

function colName(index) {
  let n = index + 1;
  let out = "";
  while (n) {
    n -= 1;
    out = String.fromCharCode(65 + (n % 26)) + out;
    n = Math.floor(n / 26);
  }
  return out;
}

function colIndex(name) {
  let value = 0;
  for (const char of String(name).toUpperCase()) value = value * 26 + char.charCodeAt(0) - 64;
  return value - 1;
}

function address(row, col) {
  return `${colName(col)}${row + 1}`;
}

function excelSerialToIso(value) {
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  if (typeof value === "number" && value > 20000 && value < 80000) {
    const ms = Date.UTC(1899, 11, 30) + Math.round(value) * 86400000;
    return new Date(ms).toISOString().slice(0, 10);
  }
  const match = text(value).match(/(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})/);
  return match ? `${match[1]}-${match[2].padStart(2, "0")}-${match[3].padStart(2, "0")}` : null;
}

function isoToSerial(value) {
  const ms = Date.parse(`${value}T00:00:00Z`);
  if (!Number.isFinite(ms)) throw new Error(`Invalid ISO date: ${value}`);
  return Math.round((ms - Date.UTC(1899, 11, 30)) / 86400000);
}

function usedValues(sheet) {
  const range = sheet.getUsedRange(true) ?? sheet.getRange("A1");
  return { range, values: range.values ?? [[null]], formulas: range.formulas ?? [[null]] };
}

function findCell(values, predicates) {
  for (let r = 0; r < values.length; r += 1) {
    for (let c = 0; c < (values[r]?.length ?? 0); c += 1) {
      const v = key(values[r][c]);
      if (predicates.some((predicate) => predicate(v, values[r][c]))) return { row: r, col: c };
    }
  }
  return null;
}

function locateTemplateModel(workbook) {
  const sheets = workbook.worksheets.items.map((sheet, index) => {
    const { range, values, formulas } = usedValues(sheet);
    return { sheet, index, name: sheet.name, address: range.address ?? null, values, formulas };
  });

  let report = null;
  for (const candidate of sheets) {
    const cost = findCell(candidate.values, [(v) => v.includes("付费总费用")]);
    const sales = findCell(candidate.values, [(v) => v.includes("真实销售额")]);
    const roi = findCell(candidate.values, [(v) => v.includes("综合投产比")]);
    if (cost && sales && roi) {
      report = { ...candidate, cost, sales, roi };
      break;
    }
  }
  if (!report) throw new Error("Template report sheet not found by semantic headers");

  const productHeader = findCell(report.values, [(v) => v.includes("产品") && !v.includes("投产")]);
  const storeHeader = findCell(report.values, [(v) => v === "店铺" || v.includes("店铺名称")]);
  if (!productHeader) throw new Error("Template product column not found");
  const dataStart = Math.max(report.cost.row, report.sales.row, report.roi.row, productHeader.row) + 1;
  let totalRow = -1;
  for (let r = dataStart; r < report.values.length; r += 1) {
    if (key(report.values[r]?.[productHeader.col]) === "合计") {
      totalRow = r;
      break;
    }
  }
  if (totalRow < 0) throw new Error("Template total row not found");
  const products = [];
  for (let r = dataStart; r < totalRow; r += 1) {
    const product = text(report.values[r]?.[productHeader.col]);
    if (!product) continue;
    products.push({
      name: product,
      row: r + 1,
      product_cell: address(r, productHeader.col),
      cost_cell: address(r, report.cost.col),
      sales_cell: address(r, report.sales.col),
      roi_cell: address(r, report.roi.col),
    });
  }
  if (!products.length) throw new Error("Template has no product rows");

  let dateCell = null;
  let templateDate = null;
  for (let r = 0; r < dataStart; r += 1) {
    for (let c = 0; c < (report.values[r]?.length ?? 0); c += 1) {
      const iso = excelSerialToIso(report.values[r][c]);
      if (iso) {
        dateCell = address(r, c);
        templateDate = iso;
        break;
      }
    }
    if (dateCell) break;
  }

  let skuSheet = null;
  for (const candidate of sheets) {
    const single = findCell(candidate.values, [(v) => v.includes("单盒sku")]);
    const triple = findCell(candidate.values, [(v) => v.includes("三盒sku")]);
    const product = findCell(candidate.values, [(v) => v.includes("商品名称")]);
    if (single && triple && product && single.row === triple.row) {
      skuSheet = { ...candidate, single, triple, product };
      break;
    }
  }

  const skuProducts = [];
  const skuMap = {};
  const skuConflicts = [];
  let skuColumns = null;
  let secondaryValueRange = null;
  if (skuSheet) {
    const headerRow = skuSheet.single.row;
    const brush = findCell(skuSheet.values.slice(headerRow, headerRow + 1), [(v) => v.includes("刷单金额")]);
    const gross = findCell(skuSheet.values.slice(headerRow, headerRow + 1), [(v) => v.includes("销售总计")]);
    const real = findCell(skuSheet.values.slice(headerRow, headerRow + 1), [(v) => v.includes("真实销售额")]);
    const brushCol = brush?.col ?? null;
    let grossCol = gross?.col ?? null;
    const realCol = real?.col ?? null;
    // The template's existing real-sales formula is the runtime truth for the
    // actual gross-sales input column.  Some validated templates carry a label
    // in another column while formulas intentionally read an adjacent input.
    if (realCol !== null) {
      const sampleFormula = text(skuSheet.formulas?.[headerRow + 1]?.[realCol]);
      const refs = [...sampleFormula.matchAll(/\$?([A-Z]{1,3})\$?\d+/gi)].map((match) => colIndex(match[1]));
      const precedent = refs.find((col) => col !== brushCol);
      if (precedent !== undefined) grossCol = precedent;
    }
    skuColumns = { header_row: headerRow + 1, product: skuSheet.product.col, single: skuSheet.single.col, triple: skuSheet.triple.col, brush: brushCol, gross: grossCol, real: realCol };
    for (let r = headerRow + 1; r < skuSheet.values.length; r += 1) {
      const productName = text(skuSheet.values[r]?.[skuSheet.product.col]);
      if (!productName) break;
      const singleSku = text(skuSheet.values[r]?.[skuSheet.single.col]);
      const tripleSku = text(skuSheet.values[r]?.[skuSheet.triple.col]);
      const item = {
        name: productName,
        row: r + 1,
        single_sku: singleSku,
        triple_sku: tripleSku,
        gross_cell: grossCol === null ? null : address(r, grossCol),
        brush_cell: brushCol === null ? null : address(r, brushCol),
        real_cell: realCol === null ? null : address(r, realCol),
        brush_value: brushCol === null ? null : skuSheet.values[r]?.[brushCol] ?? null,
      };
      skuProducts.push(item);
      for (const [sku, spec] of [[singleSku, "single"], [tripleSku, "triple"]]) {
        if (!sku) continue;
        const existing = skuMap[sku];
        if (existing && key(existing.product) !== key(productName)) {
          const conflict = skuConflicts.find((item) => item.sku === sku) ?? { sku, products: [existing.product] };
          if (!conflict.products.some((item) => key(item) === key(productName))) conflict.products.push(productName);
          if (!skuConflicts.some((item) => item.sku === sku)) skuConflicts.push(conflict);
          continue;
        }
        if (!existing) skuMap[sku] = { product: productName, spec };
      }
    }
    let best = null;
    const width = Math.max(...skuSheet.values.map((row) => row?.length ?? 0), 0);
    for (let c = (realCol ?? 0) + 1; c < width - 1; c += 1) {
      const matchingRows = [];
      for (let r = 0; r < skuSheet.values.length; r += 1) {
        const label = text(skuSheet.values[r]?.[c]);
        const value = skuSheet.values[r]?.[c + 1];
        const formula = text(skuSheet.formulas?.[r]?.[c + 1]);
        if (label && (typeof value === "number" || formula.startsWith("="))) matchingRows.push(r);
      }
      if (!best || matchingRows.length > best.rows.length) best = { col: c + 1, rows: matchingRows };
    }
    if (best && best.rows.length >= 5) {
      secondaryValueRange = `${address(Math.min(...best.rows), best.col)}:${address(Math.max(...best.rows), best.col)}`;
    }
  }

  const productNames = new Set(products.map((item) => key(item.name)));
  const productByKey = new Map(products.map((item) => [key(item.name), item.name]));
  const identityMap = {};
  const identityConflicts = [];
  function registerIdentity(kind, value, product) {
    const identity = text(value);
    if (!kind || !identity || !product) return;
    identityMap[kind] ??= {};
    const existing = identityMap[kind][identity];
    if (existing && key(existing.product) !== key(product)) {
      let conflict = identityConflicts.find((item) => item.identity_type === kind && item.value === identity);
      if (!conflict) {
        conflict = { identity_type: kind, value: identity, products: [existing.product] };
        identityConflicts.push(conflict);
      }
      if (!conflict.products.some((item) => key(item) === key(product))) conflict.products.push(product);
      return;
    }
    if (!existing) identityMap[kind][identity] = { product };
  }
  for (const [sku, mapped] of Object.entries(skuMap)) registerIdentity("sku", sku, mapped.product);
  for (const candidate of sheets) {
    for (let r = 0; r < candidate.values.length; r += 1) {
      const row = candidate.values[r] ?? [];
      const productCol = row.findIndex((value) => ["商品名称", "产品名称", "产品"].includes(key(value)));
      const identityCols = row
        .map((value, col) => ({ col, kind: identityKind(value) }))
        .filter((item) => item.kind);
      if (productCol < 0 || !identityCols.length) continue;
      for (let rr = r + 1; rr < candidate.values.length; rr += 1) {
        const rawProduct = text(candidate.values[rr]?.[productCol]);
        if (!rawProduct) break;
        const product = productByKey.get(key(rawProduct));
        if (!product) continue;
        for (const item of identityCols) registerIdentity(item.kind, candidate.values[rr]?.[item.col], product);
      }
    }
  }
  const storeGroups = [];
  for (const candidate of sheets) {
    if (candidate.name === report.name || candidate.name === skuSheet?.name) continue;
    let current = null;
    for (const row of candidate.values) {
      const first = text(row?.[0]);
      const second = text(row?.[1]);
      if (first) {
        current = { store: first, products: [] };
        storeGroups.push(current);
      }
      if (current && second) current.products.push(second);
    }
  }

  const storeValues = [];
  if (storeHeader) {
    let current = "";
    for (let r = dataStart; r < totalRow; r += 1) {
      const value = text(report.values[r]?.[storeHeader.col]);
      if (value) current = value;
      if (current) storeValues.push({ row: r + 1, store: current, product: text(report.values[r]?.[productHeader.col]) });
    }
  }

  return {
    schema_version: 1,
    sheets: sheets.map(({ name, index, address: used }) => ({ name, index, used_range: used })),
    report: {
      sheet: report.name,
      date_cell: dateCell,
      template_date: templateDate,
      product_header_cell: address(productHeader.row, productHeader.col),
      cost_header_cell: address(report.cost.row, report.cost.col),
      sales_header_cell: address(report.sales.row, report.sales.col),
      roi_header_cell: address(report.roi.row, report.roi.col),
      total_row: totalRow + 1,
      total_product_cell: address(totalRow, productHeader.col),
      total_cost_cell: address(totalRow, report.cost.col),
      total_sales_cell: address(totalRow, report.sales.col),
      total_roi_cell: address(totalRow, report.roi.col),
      products,
      store_rows: storeValues,
    },
    sku: skuSheet ? { sheet: skuSheet.name, columns: skuColumns, products: skuProducts, map: skuMap, conflicts: skuConflicts, secondary_value_range: secondaryValueRange } : null,
    identity: { map: identityMap, conflicts: identityConflicts },
    store_groups: storeGroups,
    observed: { product_count: products.length, sku_count: Object.keys(skuMap).length },
  };
}

async function importWorkbook(inputPath) {
  return SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
}

async function inspectTemplate(inputPath) {
  const workbook = await importWorkbook(inputPath);
  return locateTemplateModel(workbook);
}

async function inspectGeneric(inputPath) {
  const workbook = await importWorkbook(inputPath);
  const sheets = workbook.worksheets.items.map((sheet) => {
    const { range, values, formulas } = usedValues(sheet);
    return { name: sheet.name, used_range: range.address ?? null, values, formulas };
  });
  return { schema_version: 1, sheets };
}

function money(cents) {
  const sign = cents < 0 ? "-" : "";
  const n = Math.abs(Number(cents));
  return `${sign}${Math.floor(n / 100)}.${String(n % 100).padStart(2, "0")}`;
}

function brushMaterialization(entry) {
  if (!Object.prototype.hasOwnProperty.call(entry, "brush_cents") || entry.brush_cents === null) {
    throw new Error(`Sales payload has no brushing amount for product: ${entry.product}`);
  }
  const amount = Number(entry.brush_cents);
  if (!Number.isSafeInteger(amount)) throw new Error(`Invalid brushing cents for product: ${entry.product}`);
  const mode = entry.brush_materialization;
  const state = entry.brush_business_state;
  const provenance = entry.brush_provenance;
  if (mode === "PRESERVE") {
    if (amount !== 0 || state !== "KNOWN_ZERO" || provenance !== "DERIVED_NO_EXPLICIT_PRODUCT_FACT") {
      throw new Error(`Invalid preserved brushing contract for product: ${entry.product}`);
    }
  } else if (mode === "WRITE_ZERO") {
    if (amount !== 0 || state !== "KNOWN_ZERO" || provenance !== "HUMAN_CONFIRMED_ZERO") {
      throw new Error(`Invalid explicit-zero brushing contract for product: ${entry.product}`);
    }
  } else if (mode === "WRITE_AMOUNT") {
    if (amount === 0 || state !== "KNOWN_AMOUNT" || !["SOURCE", "HUMAN_PROVIDED"].includes(provenance)) {
      throw new Error(`Invalid nonzero brushing contract for product: ${entry.product}`);
    }
  } else {
    throw new Error(`Unknown brushing materialization for product: ${entry.product}`);
  }
  return mode;
}

function hasWorkbookValue(value) {
  return value !== null && value !== undefined && value !== "";
}

function sumFormula(cells) {
  return `=SUM(${cells[0]}:${cells[cells.length - 1]})`;
}

async function writeWorkbook(templatePath, payloadPath, outputPath) {
  const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
  const workbook = await importWorkbook(templatePath);
  const model = locateTemplateModel(workbook);
  const reportSheet = workbook.worksheets.getItem(model.report.sheet);
  const products = new Map(model.report.products.map((item) => [item.name, item]));
  if (payload.update_template_date) {
    if (!model.report.date_cell) throw new Error("Template date cell was not found");
    reportSheet.getRange(model.report.date_cell).values = [[isoToSerial(payload.target_date)]];
  }

  for (const entry of payload.product_expenses) {
    const target = products.get(entry.product);
    if (!target) throw new Error(`Output product absent from template: ${entry.product}`);
    const parts = entry.components_cents.length ? entry.components_cents.map(money) : ["0.00"];
    reportSheet.getRange(target.cost_cell).formulas = [[`=${parts.join("+")}`]];
  }

  const skuProducts = new Map((model.sku?.products ?? []).map((item) => [item.name, item]));
  if (payload.sales?.write) {
    if (!model.sku) throw new Error("Sales write requested but template has no SKU model");
    const skuSheet = workbook.worksheets.getItem(model.sku.sheet);
    for (const entry of payload.sales.products) {
      const target = skuProducts.get(entry.product);
      if (!target?.gross_cell || !target?.real_cell || !target?.brush_cell) {
        throw new Error(`Incomplete sales target for product: ${entry.product}`);
      }
      const brushMode = brushMaterialization(entry);
      const parts = entry.components_cents.length ? entry.components_cents.map(money) : ["0.00"];
      skuSheet.getRange(target.gross_cell).formulas = [[`=ROUND(${parts.join("+")},2)`]];
      if (brushMode !== "PRESERVE") {
        skuSheet.getRange(target.brush_cell).values = [[Number(money(entry.brush_cents))]];
      }
      skuSheet.getRange(target.real_cell).formulas = [[`=ROUND(${target.gross_cell}-${target.brush_cell},2)`]];
      const reportTarget = products.get(entry.product);
      if (!reportTarget) throw new Error(`Sales product absent from report: ${entry.product}`);
      reportSheet.getRange(reportTarget.sales_cell).formulas = [[`='${model.sku.sheet}'!${target.real_cell}`]];
    }
    const firstSku = model.sku.products[0];
    const lastSku = model.sku.products[model.sku.products.length - 1];
    skuSheet.getRange(`${firstSku.gross_cell}:${lastSku.gross_cell}`).format.numberFormat = "0.00";
    skuSheet.getRange(`${firstSku.brush_cell}:${lastSku.brush_cell}`).format.numberFormat = "0.00";
    skuSheet.getRange(`${firstSku.real_cell}:${lastSku.real_cell}`).format.numberFormat = "0.00";
    if (model.sku.secondary_value_range) skuSheet.getRange(model.sku.secondary_value_range).format.numberFormat = "0.00";
  }

  for (const target of model.report.products) {
    reportSheet.getRange(target.roi_cell).formulas = [[`=IF(${target.cost_cell}=0,0,${target.sales_cell}/${target.cost_cell})`]];
  }
  const first = model.report.products[0];
  const last = model.report.products[model.report.products.length - 1];
  reportSheet.getRange(model.report.total_cost_cell).formulas = [[sumFormula([first.cost_cell, last.cost_cell])]];
  reportSheet.getRange(model.report.total_sales_cell).formulas = [[sumFormula([first.sales_cell, last.sales_cell])]];
  reportSheet.getRange(model.report.total_roi_cell).formulas = [[`=IF(${model.report.total_cost_cell}=0,0,${model.report.total_sales_cell}/${model.report.total_cost_cell})`]];
  reportSheet.getRange(`${first.cost_cell}:${model.report.total_sales_cell}`).format.numberFormat = "#,##0.00";
  reportSheet.getRange(`${first.roi_cell}:${model.report.total_roi_cell}`).format.numberFormat = payload.roi_number_format ?? "0.00";

  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await (await SpreadsheetFile.exportXlsx(workbook)).save(outputPath);
  return { output_path: outputPath, template_model: model };
}

function cents(value) {
  return Math.round(Number(value ?? 0) * 100);
}

async function verifyWorkbook(templatePath, outputPath, payloadPath, renderDir) {
  const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
  const template = await importWorkbook(templatePath);
  const output = await importWorkbook(outputPath);
  const before = locateTemplateModel(template);
  const after = locateTemplateModel(output);
  const failures = [];
  const sheetNamesBefore = before.sheets.map((s) => s.name);
  const sheetNamesAfter = after.sheets.map((s) => s.name);
  if (JSON.stringify(sheetNamesBefore) !== JSON.stringify(sheetNamesAfter)) failures.push("sheet_names_or_order_changed");
  if (JSON.stringify(before.report.products.map((p) => p.name)) !== JSON.stringify(after.report.products.map((p) => p.name))) failures.push("product_order_changed");
  if (JSON.stringify(before.sku?.map ?? {}) !== JSON.stringify(after.sku?.map ?? {})) failures.push("sku_mapping_changed");
  if (JSON.stringify(before.identity?.map ?? {}) !== JSON.stringify(after.identity?.map ?? {})) failures.push("product_identity_mapping_changed");

  const report = output.worksheets.getItem(after.report.sheet);
  const expectedExpense = payload.product_expenses.reduce((sum, item) => sum + item.components_cents.reduce((a, b) => a + b, 0), 0);
  const actualExpense = cents(report.getRange(after.report.total_cost_cell).values[0][0]);
  if (actualExpense !== expectedExpense) failures.push(`expense_total:${actualExpense}!=${expectedExpense}`);
  let actualRealSales = null;
  if (payload.sales?.write) {
    const beforeSkuProducts = new Map((before.sku?.products ?? []).map((item) => [item.name, item]));
    const afterSkuProducts = new Map((after.sku?.products ?? []).map((item) => [item.name, item]));
    for (const entry of payload.sales.products) {
      const mode = brushMaterialization(entry);
      const beforeProduct = beforeSkuProducts.get(entry.product);
      const afterProduct = afterSkuProducts.get(entry.product);
      if (!beforeProduct || !afterProduct) {
        failures.push(`brushing_product_missing:${entry.product}`);
        continue;
      }
      const beforeValue = beforeProduct.brush_value;
      const afterValue = afterProduct.brush_value;
      if (mode === "PRESERVE") {
        if (JSON.stringify(afterValue) !== JSON.stringify(beforeValue)) {
          failures.push(`brush_representation:${entry.product}:not_preserved`);
        }
      } else if (!hasWorkbookValue(afterValue)) {
        failures.push(`brush_representation:${entry.product}:missing`);
      } else {
        const actualBrush = cents(afterValue);
        if (actualBrush !== entry.brush_cents) {
          failures.push(`brush_amount:${entry.product}:${actualBrush}!=${entry.brush_cents}`);
        }
      }
    }
    const expectedReal = payload.sales.real_sales_cents;
    actualRealSales = cents(report.getRange(after.report.total_sales_cell).values[0][0]);
    if (actualRealSales !== expectedReal) failures.push(`real_sales_total:${actualRealSales}!=${expectedReal}`);
  }

  for (const product of after.report.products) {
    const formula = text(report.getRange(product.roi_cell).formulas[0][0]).toUpperCase();
    const wanted = `=IF(${product.cost_cell}=0,0,${product.sales_cell}/${product.cost_cell})`.toUpperCase();
    if (formula !== wanted) failures.push(`roi_formula:${product.roi_cell}`);
  }
  const totalRoi = text(report.getRange(after.report.total_roi_cell).formulas[0][0]).toUpperCase();
  const wantedTotalRoi = `=IF(${after.report.total_cost_cell}=0,0,${after.report.total_sales_cell}/${after.report.total_cost_cell})`.toUpperCase();
  if (totalRoi !== wantedTotalRoi) failures.push("total_roi_formula");

  const errors = await output.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 500 },
    summary: "daily ROI formula error scan",
  });
  const errorLines = text(errors.ndjson).split(/\r?\n/).filter((line) => /#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A/.test(line));
  if (errorLines.length) failures.push(`formula_errors:${errorLines.length}`);

  const rendered = [];
  if (renderDir) {
    await fs.mkdir(renderDir, { recursive: true });
    for (const sheet of output.worksheets.items) {
      const image = await output.render({ sheetName: sheet.name, autoCrop: "all", scale: 1.5, format: "png" });
      const target = path.join(renderDir, `${sheet.name}.png`);
      await fs.writeFile(target, new Uint8Array(await image.arrayBuffer()));
      rendered.push(target);
    }
  }
  return {
    status: failures.length ? "FAIL" : "PASS",
    failures,
    actual_expense_cents: actualExpense,
    actual_real_sales_cents: actualRealSales,
    formula_error_count: errorLines.length,
    rendered_sheets: rendered,
    visual_verification_level: rendered.length ? "RENDERED_UNREVIEWED" : "NOT_RENDERED",
    structure: { sheet_names: sheetNamesAfter, product_count: after.observed.product_count, sku_count: after.observed.sku_count },
  };
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  let result;
  if (command === "inspect-template") result = await inspectTemplate(args[0]);
  else if (command === "inspect-xlsx") result = await inspectGeneric(args[0]);
  else if (command === "write") result = await writeWorkbook(args[0], args[1], args[2]);
  else if (command === "verify") result = await verifyWorkbook(args[0], args[1], args[2], args[3]);
  else throw new Error(`Unknown command: ${command}`);
  if (args[command === "write" ? 3 : command === "verify" ? 4 : 1]) {
    const outputJson = args[command === "write" ? 3 : command === "verify" ? 4 : 1];
    await fs.mkdir(path.dirname(outputJson), { recursive: true });
    await fs.writeFile(outputJson, JSON.stringify(result, null, 2), "utf8");
  } else {
    process.stdout.write(`${JSON.stringify(result)}\n`);
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack ?? error}\n`);
  process.exitCode = 1;
});
