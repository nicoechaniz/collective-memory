import { Archive, Atom, BookOpen, FlaskConical, LogOut, Menu, Search, Telescope, X } from "lucide-react";
import { useEffect, useState } from "react";
import AtlasPage from "./AtlasPage";
import DocumentReader from "./DocumentReader";
import { request, serviceLink, v2 } from "./api";
import { ArchivePage, SearchPage } from "./ReadPages";
import { navigate, queryParams, routeFor, updateQuery, useLocationKey, type Route } from "./router";
import { SessionProvider, useSession } from "./session";
import type { Bootstrap, Manifest } from "./types";
import DirectorPage from "./lab/DirectorPage";
import DiscoverPage from "./lab/DiscoverPage";
import EvidencePage from "./lab/EvidencePage";
import LoginGate from "./lab/LoginGate";
import TrayPage from "./lab/TrayPage";
import { STRUCTURAL_NOTICE, STRUCTURAL_ONLY } from "./lab/constants";

const MODE = __MAPA_APP_MODE__;

function Brand() {
  return <span className="v3-brand"><span className="v3-brand-mark">MC</span><span><b>Memoria Colectiva</b><small>Observatorio de conocimiento</small></span></span>;
}

function Topbar({ route, immersive, onNavigate, children }: {
  route: Route;
  immersive?: boolean;
  onNavigate: (route: Route) => void;
  children?: React.ReactNode;
}) {
  const [menu, setMenu] = useState(false);
  if (immersive) return null;
  const atlasNav: [Route, string, typeof Telescope][] = [["atlas", "Atlas", Telescope], ["buscar", "Buscar", Search], ["archivo", "Archivo", Archive]];
  const labNav: [Route, string, typeof FlaskConical][] = [["descubrir", "Descubrir", FlaskConical], ["bandeja", "Bandeja", BookOpen], ["evidencia", "Evidencia", Atom], ["director", "Director", Telescope]];
  const visibleLabNav = STRUCTURAL_ONLY ? labNav.filter(([id]) => id !== "director") : labNav;
  const items = MODE === "atlas" ? atlasNav : visibleLabNav;
  return <header className="v3-topbar">
    <button className="v3-brand-button" onClick={() => onNavigate(MODE === "atlas" ? "atlas" : "descubrir")}><Brand /></button>
    <nav className={menu ? "open" : ""} aria-label="Navegación principal">{items.map(([id, label, Icon]) => <button key={id} className={route === id ? "active" : ""} onClick={() => { onNavigate(id); setMenu(false); }}><Icon /><span>{label}</span></button>)}</nav>
    <div className="v3-top-actions">{MODE === "atlas" ? <a href={serviceLink("lab", "/lab-v3/descubrir")}><FlaskConical /> Lab</a> : <><a href={serviceLink("atlas", "/atlas-v3/")}><Telescope /> Atlas</a><a className="utility" href={serviceLink("atlas", "/atlas-v3/buscar")}><Search /><span>Buscar</span></a><a className="utility" href={serviceLink("atlas", "/atlas-v3/archivo")}><Archive /><span>Archivo</span></a></>}{children}<button className="v3-menu-button" aria-label={menu ? "Cerrar menú" : "Abrir menú"} onClick={() => setMenu((value) => !value)}>{menu ? <X /> : <Menu />}</button></div>
  </header>;
}

function Loading() {
  return <main className="v3-loading"><div className="v3-compass" /><p>Levantando el territorio…</p></main>;
}

function StructuralUnavailable() {
  return <main className="v3-lab-page v3-unavailable"><p className="v3-eyebrow">Modo estructural</p><h1>Director no disponible.</h1><p>{STRUCTURAL_NOTICE}</p></main>;
}

function ReadApp() {
  const locationKey = useLocationKey();
  const route = routeFor("atlas");
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [error, setError] = useState("");
  const [immersive, setImmersive] = useState(false);
  const docId = queryParams().get("doc");
  const pageParams = new URLSearchParams(locationKey.split("?")[1] || "");
  pageParams.delete("doc");
  const pageKey = `${route}:${pageParams}`;

  useEffect(() => {
    Promise.all([v2<Bootstrap>("/bootstrap"), request<Manifest>("/ui/manifest")])
      .then(([boot, map]) => { setBootstrap(boot.data); setManifest(map); })
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, []);
  useEffect(() => { if (route !== "atlas") setImmersive(false); }, [route]);

  function go(next: Route) { navigate("atlas", next); }
  function openDoc(id: string) { updateQuery("doc", id); }
  function wikilink(value: string) { const params = new URLSearchParams({ q: value, limit: "40", offset: "0" }); navigate("atlas", "buscar", params); }
  function neighbors(id: string) { const params = new URLSearchParams({ view: "neighbors", nb: id }); navigate("atlas", "atlas", params); }

  return <div className={`v3-app ${immersive ? "immersive" : ""}`}>
    <Topbar route={route} immersive={immersive} onNavigate={go} />
    {route === "atlas" ? <AtlasPage immersive={immersive} setImmersive={setImmersive} /> : !bootstrap ? <Loading /> : <>
      {route === "buscar" && <SearchPage key={pageKey} bootstrap={bootstrap} manifest={manifest} onDoc={openDoc} />}
      {route === "archivo" && <ArchivePage key={pageKey} bootstrap={bootstrap} onDoc={openDoc} />}
    </>}
    {error && route !== "atlas" && <div className="v3-notice error global">{error}</div>}
    {docId && <DocumentReader docId={docId} onClose={() => updateQuery("doc", null, true)} onOpenDoc={openDoc} onNeighbors={neighbors} onWikilink={wikilink} />}
  </div>;
}

function LabWorkspace() {
  useLocationKey();
  const route = routeFor("lab");
  const { identity, logout } = useSession();
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const docId = queryParams().get("doc");

  useEffect(() => {
    request<Manifest>("/ui/manifest").then(setManifest).catch(() => setManifest(null));
  }, []);

  function go(next: Route) { navigate("lab", next); }
  function openDoc(id: string) { updateQuery("doc", id); }
  function neighbors(id: string) { window.open(serviceLink("atlas", `/atlas-v3/?view=neighbors&nb=${encodeURIComponent(id)}`), "_blank", "noopener,noreferrer"); }
  function wikilink(value: string) { window.open(serviceLink("atlas", `/atlas-v3/buscar?q=${encodeURIComponent(value)}&limit=40&offset=0`), "_blank", "noopener,noreferrer"); }

  return <div className="v3-app v3-lab-app">
    <Topbar route={route} onNavigate={go}>{identity && <button className="v3-session" onClick={logout} title={`Cerrar sesión de ${identity.user}`}><span>{identity.user}</span><LogOut /></button>}</Topbar>
    {identity === undefined ? <Loading /> : identity === null ? <LoginGate /> : <>
      {route === "descubrir" && <DiscoverPage manifest={manifest} />}
      {route === "bandeja" && <TrayPage onDoc={openDoc} />}
      {route === "evidencia" && <EvidencePage onDoc={openDoc} />}
      {route === "director" && (STRUCTURAL_ONLY ? <StructuralUnavailable /> : <DirectorPage />)}
    </>}
    {docId && identity && <DocumentReader docId={docId} onClose={() => updateQuery("doc", null, true)} onOpenDoc={openDoc} onNeighbors={neighbors} onWikilink={wikilink} />}
  </div>;
}

export default function App() {
  return MODE === "atlas" ? <ReadApp /> : <SessionProvider><LabWorkspace /></SessionProvider>;
}
