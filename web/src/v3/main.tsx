import "@fontsource-variable/fraunces/wght.css";
import "@fontsource/ibm-plex-sans/latin-ext-400.css";
import "@fontsource/ibm-plex-sans/latin-ext-500.css";
import "@fontsource/ibm-plex-sans/latin-ext-600.css";
import "@fontsource/ibm-plex-mono/latin-ext-400.css";
import "@fontsource/ibm-plex-mono/latin-ext-500.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ENGLISH_UI, t } from "../i18n";
import "./styles.css";

document.documentElement.lang = ENGLISH_UI ? "en" : "es";
document.title = __MAPA_APP_MODE__ === "atlas"
  ? t("Atlas · Memoria Colectiva", "Atlas · Collective Memory")
  : t("Laboratorio · Memoria Colectiva", "Laboratory · Collective Memory");

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
