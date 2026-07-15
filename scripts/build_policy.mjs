// Сборка публикуемого DOCX политики конфиденциальности из мастер-текста
// docs/Политика_конфиденциальности_ИС_Чат-бот_MAX.md по Правилам
// юридико-технического оформления МПА (юртех):
//   Times New Roman 14, поля 30/10/20/20 мм, одинарный интервал, по ширине,
//   отступ первой строки 1,25 см, угловой реквизит приложения 12 пт справа,
//   наименование по центру полужирным, разделы по центру обычным начертанием,
//   номер страницы сверху по центру со второй страницы.
// Всё после маркера ПРИЛОЖЕНИЕ-А (чек-лист юриста) в сборку не попадает.
//
// Запуск:  node scripts/build_policy.mjs
// Выход:   docs/Политика_конфиденциальности_ИС_Чат-бот_MAX.docx
// PDF затем: soffice --headless --convert-to pdf <docx>

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const {
  Document, Packer, Paragraph, TextRun, AlignmentType, Header, PageNumber,
} = require("docx");

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")), "..");
const SRC = path.join(ROOT, "docs", "Политика_конфиденциальности_ИС_Чат-бот_MAX.md");
const OUT = path.join(ROOT, "docs", "Политика_конфиденциальности_ИС_Чат-бот_MAX.docx");

// мм -> twips (1 мм = 56.6929 twips)
const MM = (v) => Math.round(v * 56.6929);
const FONT = "Times New Roman";
const SZ14 = 28; // half-points
const SZ12 = 24;
const INDENT_125CM = 709; // 1.25 cm in twips

let text = fs.readFileSync(SRC, "utf-8");
// отрезать чек-лист юриста (маркер — отдельный комментарий, не упоминание в шапке)
const cut = text.indexOf("<!-- ПРИЛОЖЕНИЕ-А");
if (cut !== -1) text = text.slice(0, cut);
// убрать html-комментарии
text = text.replace(/<!--[\s\S]*?-->/g, "");

const block = (name) => {
  const m = text.match(new RegExp(`\\[${name}\\]([\\s\\S]*?)\\[\\/${name}\\]`));
  return m ? m[1].trim().split(/\r?\n/).map((s) => s.trim()).filter(Boolean) : [];
};
const corner = block("УГЛОВОЙ-РЕКВИЗИТ");
const title = block("НАИМЕНОВАНИЕ");
const body = text
  .replace(/\[УГЛОВОЙ-РЕКВИЗИТ\][\s\S]*?\[\/УГЛОВОЙ-РЕКВИЗИТ\]/, "")
  .replace(/\[НАИМЕНОВАНИЕ\][\s\S]*?\[\/НАИМЕНОВАНИЕ\]/, "");

const children = [];

// Угловой реквизит: 12 пт, блок прижат вправо (левый отступ), одинарный
for (const line of corner) {
  children.push(new Paragraph({
    children: [new TextRun({ text: line, font: FONT, size: SZ12 })],
    indent: { left: MM(95) },
    spacing: { after: 0, line: 240, lineRule: "auto" },
  }));
}
children.push(new Paragraph({ children: [], spacing: { after: 0 } }));

// Наименование: по центру, полужирное, 14; первое слово — отдельной строкой
for (const line of title) {
  children.push(new Paragraph({
    children: [new TextRun({ text: line, font: FONT, size: SZ14, bold: true })],
    alignment: AlignmentType.CENTER,
    spacing: { after: 0, line: 240 },
  }));
}
children.push(new Paragraph({ children: [], spacing: { after: 0 } }));

// Тело: заголовки разделов (## N. Название) по центру обычным начертанием;
// остальные непустые строки — абзацы по ширине с отступом 1,25 см
for (const raw of body.split(/\r?\n/)) {
  const line = raw.trim();
  if (!line) continue;
  const h = line.match(/^##\s+(.*)$/);
  if (h) {
    children.push(new Paragraph({ children: [], spacing: { after: 0 } }));
    children.push(new Paragraph({
      children: [new TextRun({ text: h[1], font: FONT, size: SZ14 })],
      alignment: AlignmentType.CENTER,
      spacing: { after: 0, line: 240 },
    }));
    children.push(new Paragraph({ children: [], spacing: { after: 0 } }));
    continue;
  }
  children.push(new Paragraph({
    children: [new TextRun({ text: line, font: FONT, size: SZ14 })],
    alignment: AlignmentType.JUSTIFIED,
    indent: { firstLine: INDENT_125CM },
    spacing: { after: 0, line: 240, lineRule: "auto" },
  }));
}

const pageNumberHeader = new Header({
  children: [new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: SZ14 })],
  })],
});

const doc = new Document({
  styles: { default: { document: { run: { font: FONT, size: SZ14 } } } },
  sections: [{
    properties: {
      page: {
        size: { width: MM(210), height: MM(297) },
        margin: { top: MM(20), bottom: MM(20), left: MM(30), right: MM(10) },
      },
      titlePage: true, // на первой странице номер не проставляется (юртех п. 11)
    },
    headers: { default: pageNumberHeader, first: new Header({ children: [] }) },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(OUT, buf);
  console.log("OK:", OUT, buf.length, "bytes;", children.length, "paragraphs");
});
