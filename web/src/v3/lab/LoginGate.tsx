import { KeyRound, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { useSession } from "../session";
import { STRUCTURAL_NOTICE, STRUCTURAL_ONLY } from "./constants";
import { t } from "../../i18n";

export default function LoginGate() {
  const { identity, error: sessionError, login } = useSession();
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (identity !== null) return null;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError("");
    try {
      await login(token.trim());
      setToken("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return <main className="v3-login">
    <section className="v3-login-card">
      <div className="v3-login-mark"><KeyRound /></div>
      <p className="v3-eyebrow">{t("Acceso personal", "Personal access")}</p>
      <h1>{t("Entrá al laboratorio.", "Enter the laboratory.")}</h1>
      <p>{t("El token se intercambia por una sesión HttpOnly. No queda guardado en el navegador.", "The token is exchanged for an HttpOnly session. It is never stored in the browser.")}</p>
      {STRUCTURAL_ONLY && <div className="v3-structural-note"><ShieldCheck /><span><b>{t("Demostración sin inferencia LLM en vivo", "Demo without live LLM inference")}</b>{STRUCTURAL_NOTICE}</span></div>}
      <form onSubmit={submit}>
        <label>{t("Token personal", "Personal token")}<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" spellCheck={false} autoFocus /></label>
        <button className="v3-primary" disabled={!token.trim() || busy}>{busy ? t("Creando sesión…", "Creating session…") : t("Crear sesión", "Create session")}</button>
      </form>
      {(error || sessionError) && <div className="v3-notice error">{error || sessionError}</div>}
      <footer><ShieldCheck /> {t("Los candidatos permanecen en tu sandbox hasta que los propongas al flujo editorial.", "Candidates remain in your sandbox until you submit them to the editorial workflow.")}</footer>
    </section>
  </main>;
}
