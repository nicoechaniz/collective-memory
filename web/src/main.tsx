import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ENGLISH_UI, t } from "./i18n";
import "./styles.css";

document.documentElement.lang = ENGLISH_UI ? "en" : "es";
document.title = t("Atlas — memoria colectiva", "Atlas — Collective Memory");

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
