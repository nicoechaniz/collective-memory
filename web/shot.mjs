// Captura la UI del Lab tal como la ve el usuario (token inyectado en localStorage).
// Uso: node shot.mjs <token> [outdir]
import { chromium } from "playwright";

const TOKEN = process.argv[2];
const OUT = process.argv[3] || "/tmp/shots";
const BASE = process.env.MAPA_LAB_URL || "http://127.0.0.1:8898/lab/";

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

// sembrar el token antes de que cargue la SPA
await page.addInitScript((t) => localStorage.setItem("lab.token", t), TOKEN);
page.on("pageerror", (e) => console.log("JS ERROR:", e.message));
page.on("console", (m) => m.type() === "error" && console.log("CONSOLE:", m.text().slice(0, 120)));

await page.goto(BASE, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);
await page.screenshot({ path: `${OUT}/1-correr.png` });

// Bandeja + abrir el primer candidato
await page.getByRole("button", { name: "Bandeja" }).click();
await page.waitForTimeout(600);
await page.getByRole("button", { name: "de todos" }).first().click();  // ver candidatos reales
await page.waitForTimeout(900);
const firstRow = page.locator("tbody tr").first();
if (await firstRow.count()) {
  await firstRow.locator("td").nth(2).click();   // celda del título
  await page.waitForTimeout(1200);
}
await page.screenshot({ path: `${OUT}/2-bandeja.png` });

// medir qué tan alta es la barra de acciones vs el área de lectura
const m = await page.evaluate(() => {
  const bar = document.querySelector(".actionbar:last-of-type");
  const scroll = [...document.querySelectorAll("div")].find(
    (d) => d.style.overflowY === "auto" && d.style.padding === "16px");
  const btn = document.querySelector(".actionbar button");
  return {
    barH: bar?.getBoundingClientRect().height ?? null,
    lecturaH: scroll?.getBoundingClientRect().height ?? null,
    btn: btn ? { w: Math.round(btn.getBoundingClientRect().width), h: Math.round(btn.getBoundingClientRect().height) } : null,
    scrollable: scroll ? scroll.scrollHeight > scroll.clientHeight : null,
  };
});
console.log("MEDIDAS:", JSON.stringify(m));

// Grafo
await page.getByRole("button", { name: "Grafo" }).click();
await page.waitForTimeout(800);
await page.getByRole("button", { name: "de todos" }).first().click();
await page.waitForTimeout(3000);
await page.screenshot({ path: `${OUT}/3-grafo.png` });
const g = await page.evaluate(() => {
  const c = document.querySelector("canvas");
  const wrap = document.querySelector(".canvas-wrap");
  return { canvas: c ? { w: c.clientWidth, h: c.clientHeight } : null,
           wrap: wrap ? { w: wrap.clientWidth, h: wrap.clientHeight } : null,
           viewportH: window.innerHeight };
});
console.log("GRAFO:", JSON.stringify(g));

await browser.close();
console.log("OK →", OUT);
