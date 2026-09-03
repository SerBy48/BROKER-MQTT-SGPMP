#!/usr/bin/env node
/**
 * Se ejecuta como parte del pipeline de release (ver .releaserc.json,
 * plugin @semantic-release/exec, hook "prepareCmd").
 *
 * Recorre los commits entre la versión anterior y la nueva, extrae los
 * IDs de RF/RNF/RFC/BUG referenciados y agrega una fila a
 * TRAZABILIDAD_CAMBIOS.md.
 *
 * La extracción es deliberadamente tolerante en el formato (mayúsculas/
 * minúsculas, con o sin guión: "RF-14", "rf14", "RF 14") en vez de exigir
 * un formato exacto -- así se documenta cómo el equipo escribe los commits
 * de verdad, sin depender de que cambien un hábito ya establecido. También
 * expande el atajo "RF-15/19/20" a RF-15, RF-19, RF-20.
 *
 * Uso: node append_trazabilidad.js <nextVersion> <gitTag> <lastVersion>
 */
const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const [, , nextVersion, gitTag, lastVersion] = process.argv;
const FILE = path.join(__dirname, "..", "docs", "trazabilidad", "TRAZABILIDAD_CAMBIOS.md");

function getCommitsSinceLastRelease() {
  const range = lastVersion && lastVersion !== "undefined"
    ? `v${lastVersion}..HEAD`
    : "HEAD";
  try {
    const log = execSync(`git log ${range} --pretty=format:"%s|||%H"`, { encoding: "utf8" });
    return log.split("\n").filter(Boolean).map(line => {
      const [subject, hash] = line.split("|||");
      return { subject, hash: hash.slice(0, 7) };
    });
  } catch (e) {
    return [];
  }
}

function extractIds(subject) {
  // Case-insensitive y separador opcional (guion, espacio o nada) entre la
  // sigla y el número -- el equipo escribe "RF-14", "rf14", "RF 14" o
  // "rf-14" indistintamente, y exigir un formato exacto solo generaba
  // filas de trazabilidad vacías pese a que el commit sí referenciaba un RF.
  // También expande el atajo "RF-15/19/20" -> RF-15, RF-19, RF-20.
  const rf = [];
  const rfPattern = /\b(rf|rnf)[\s-]?(\d+)((?:\s*\/\s*\d+)*)\b/gi;
  let match;
  while ((match = rfPattern.exec(subject)) !== null) {
    const prefix = match[1].toUpperCase();
    rf.push(`${prefix}-${match[2]}`);
    const extras = match[3].match(/\d+/g) || [];
    extras.forEach(n => rf.push(`${prefix}-${n}`));
  }

  const rfc = [...subject.matchAll(/\brfc[\s-]?(\d+)\b/gi)].map(m => `RFC-${m[1]}`);
  const bug = [...subject.matchAll(/\bbug[\s-]?(\d+)\b/gi)].map(m => `BUG-${m[1]}`);
  return { rf, rfc, bug };
}

function buildRow() {
  const commits = getCommitsSinceLastRelease();
  const rfSet = new Set();
  const rfcSet = new Set();
  const bugSet = new Set();

  commits.forEach(({ subject }) => {
    const { rf, rfc, bug } = extractIds(subject);
    rf.forEach(id => rfSet.add(id));
    rfc.forEach(id => rfcSet.add(id));
    bug.forEach(id => bugSet.add(id));
  });

  const fecha = new Date().toISOString().slice(0, 10);
  const commitList = commits.length
    ? commits.map(c => `${c.hash} ${c.subject}`).join("<br>")
    : "(sin commits detectados en el rango)";

  return `| ${nextVersion} | ${gitTag || "v" + nextVersion} | ${fecha} | ${[...rfSet].join(", ") || "—"} | ${[...rfcSet].join(", ") || "—"} | ${[...bugSet].join(", ") || "—"} | ${commitList} |\n`;
}

function ensureFile() {
  if (!fs.existsSync(FILE)) {
    fs.mkdirSync(path.dirname(FILE), { recursive: true });
    const header = `# Trazabilidad de cambios\n\n` +
      `Este archivo se genera automáticamente en cada release (ver ` +
      `\`scripts/append_trazabilidad.js\` y \`.releaserc.json\`). No editar a mano.\n\n` +
      `| Versión | Tag | Fecha | RF/RNF | RFC | Bugs | Commits incluidos |\n` +
      `|---|---|---|---|---|---|---|\n`;
    fs.writeFileSync(FILE, header, "utf8");
  }
}

ensureFile();
fs.appendFileSync(FILE, buildRow(), "utf8");
console.log(`TRAZABILIDAD_CAMBIOS.md actualizado con la versión ${nextVersion}`);
