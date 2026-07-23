import fs from "node:fs";
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";

const input = process.argv[2];
const output = process.argv[3] || input;

if (!input) {
  console.error("usage: node layout.mjs <graph.json> [out.json]");
  process.exit(2);
}

const data = JSON.parse(fs.readFileSync(input, "utf8"));
const graph = new Graph({ multi: true, type: "mixed" });

function hashFloat(id, salt) {
  let h = 2166136261;
  const s = `${salt}:${id}`;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) / 4294967295) * 2 - 1;
}

const nodes = [...data.nodes].sort((a, b) => a.id.localeCompare(b.id));
const edges = [...data.edges].sort((a, b) => a.id.localeCompare(b.id));

for (const node of nodes) {
  const x = Number.isFinite(node.x) && node.x !== 0 ? node.x : hashFloat(node.id, "x") * 20;
  const y = Number.isFinite(node.y) && node.y !== 0 ? node.y : hashFloat(node.id, "y") * 20;
  graph.addNode(node.id, { x, y, size: Math.max(1, Math.log1p(node.degree || 1) * 1.8) });
}

for (const edge of edges) {
  if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
  const attrs = { weight: edge.weight || 1, type: edge.type };
  if (edge.directed) graph.addDirectedEdgeWithKey(edge.id, edge.source, edge.target, attrs);
  else graph.addUndirectedEdgeWithKey(edge.id, edge.source, edge.target, attrs);
}

forceAtlas2.assign(graph, {
  iterations: 120,
  settings: {
    gravity: 1,
    scalingRatio: 8,
    slowDown: 10,
    linLogMode: false,
    outboundAttractionDistribution: false,
    adjustSizes: false,
    barnesHutOptimize: graph.order > 500,
    barnesHutTheta: 0.5,
  },
});

const pos = {};
graph.forEachNode((id, attrs) => {
  pos[id] = { x: Number(attrs.x.toFixed(6)), y: Number(attrs.y.toFixed(6)) };
});

data.nodes = data.nodes.map((node) => ({ ...node, ...(pos[node.id] || {}) }));
fs.writeFileSync(output, JSON.stringify(data));
