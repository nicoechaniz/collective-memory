import { Maximize2, Minimize2 } from "lucide-react";
import { useEffect, useRef } from "react";
import { queryParams } from "./router";

function editable(target: EventTarget | null) {
  const element = target as HTMLElement | null;
  return Boolean(element?.closest("input,textarea,select,[contenteditable=true]"));
}

export default function AtlasPage({ immersive, setImmersive }: { immersive: boolean; setImmersive: (value: boolean) => void }) {
  const iframe = useRef<HTMLIFrameElement | null>(null);
  const params = queryParams();
  const neighbor = params.get("nb");
  const src = neighbor
    ? `/atlas/graph/#view=neighbors&nb=${encodeURIComponent(neighbor)}`
    : "/atlas/graph/#view=global&d3=1";

  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (editable(event.target)) return;
      if (event.key.toLowerCase() === "f") setImmersive(!immersive);
      if (event.key === "Escape" && immersive) setImmersive(false);
    };
    window.addEventListener("keydown", key);
    const frame = iframe.current;
    const attach = () => {
      try { frame?.contentDocument?.addEventListener("keydown", key); } catch { /* same-origin guard */ }
    };
    frame?.addEventListener("load", attach);
    attach();
    return () => {
      window.removeEventListener("keydown", key);
      frame?.removeEventListener("load", attach);
      try { frame?.contentDocument?.removeEventListener("keydown", key); } catch { /* noop */ }
    };
  }, [immersive, setImmersive]);

  return <section className="v3-atlas" aria-label="Atlas visual completo">
    <iframe ref={iframe} title="Atlas completo de Memoria Colectiva" src={src} loading="eager" />
    <button className="v3-immersive-toggle" onClick={() => setImmersive(!immersive)} title={immersive ? "Salir del modo inmersivo (F o Esc)" : "Usar todo el viewport (F)"}>{immersive ? <Minimize2 /> : <Maximize2 />}<span>{immersive ? "Salir" : "Inmersivo"}</span></button>
  </section>;
}
