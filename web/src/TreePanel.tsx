import { useEffect, useMemo, useState } from "react";
import { getJson, type TreeNode, type TreePayload } from "./api";
import { colorForProject } from "./colors";

type Props = { onOpenDoc: (docId: string) => void };

function TreeRow({ node, childrenIndex, onOpenDoc, depth }: { node: TreeNode; childrenIndex: Map<string, TreeNode[]>; onOpenDoc: (id: string) => void; depth: number }) {
  const [open, setOpen] = useState(false);
  const kids = childrenIndex.get(node.id) || [];
  const isDoc = Boolean(node.doc_id);
  return (
    <div className="tree-row" style={{ paddingLeft: depth * 12 }}>
      {isDoc ? (
        <button className="tree-doc" onClick={() => onOpenDoc(node.doc_id!)} title={node.path_rel}>
          <span className="tree-icon">·</span> {node.label}
        </button>
      ) : (
        <button className="tree-folder" onClick={() => setOpen((o) => !o)}>
          <span className="tree-icon">{open ? "▾" : "▸"}</span>
          {node.kind === "project" && <i style={{ background: colorForProject(node.project) }} />}
          {node.label} <span className="tree-count">({node.doc_count})</span>
        </button>
      )}
      {open &&
        kids.map((k) => <TreeRow key={k.id} node={k} childrenIndex={childrenIndex} onOpenDoc={onOpenDoc} depth={depth + 1} />)}
    </div>
  );
}

export default function TreePanel({ onOpenDoc }: Props) {
  const [tree, setTree] = useState<TreePayload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getJson<TreePayload>("/ui/tree").then(setTree).catch((e) => setError(String(e)));
  }, []);

  const { childrenIndex, roots } = useMemo(() => {
    const idx = new Map<string, TreeNode[]>();
    const byId = new Map<string, TreeNode>();
    if (tree) {
      for (const n of tree.nodes) byId.set(n.id, n);
      for (const n of tree.nodes) {
        if (!n.parent) continue;
        const arr = idx.get(n.parent) || [];
        arr.push(n);
        idx.set(n.parent, arr);
      }
      for (const arr of idx.values()) arr.sort((a, b) => Number(Boolean(a.doc_id)) - Number(Boolean(b.doc_id)) || b.doc_count - a.doc_count || a.label.localeCompare(b.label));
    }
    const roots = tree
      ? tree.roots
          .map((r) => byId.get(r))
          .filter((n): n is TreeNode => Boolean(n))
          .sort((a, b) => b.doc_count - a.doc_count)
      : [];
    return { childrenIndex: idx, roots };
  }, [tree]);

  return (
    <div className="tree-panel">
      {error && <div className="error">{error}</div>}
      {!tree && !error && <div className="loading">cargando árbol…</div>}
      {roots.map((r) => (
        <TreeRow key={r.id} node={r} childrenIndex={childrenIndex} onOpenDoc={onOpenDoc} depth={0} />
      ))}
      {tree && Object.keys(tree.excluded_summary || {}).length > 0 && (
        <details className="tree-excluded">
          <summary>excluidos del índice{tree.excluded_summary_stale ? " (dato viejo)" : ""}</summary>
          <ul>
            {Object.entries(tree.excluded_summary).map(([reason, n]) => (
              <li key={reason}>{reason}: {n}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
