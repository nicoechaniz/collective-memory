import { KeyRound, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { useSession } from "../session";
import { STRUCTURAL_NOTICE, STRUCTURAL_ONLY } from "./constants";

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
      <p className="v3-eyebrow">Acceso personal</p>
      <h1>Entrá al laboratorio.</h1>
      <p>El token se intercambia por una sesión HttpOnly. No queda guardado en el navegador.</p>
      {STRUCTURAL_ONLY && <div className="v3-structural-note"><ShieldCheck /><span><b>Demostración sin inferencia LLM en vivo</b>{STRUCTURAL_NOTICE}</span></div>}
      <form onSubmit={submit}>
        <label>Token personal<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" spellCheck={false} autoFocus /></label>
        <button className="v3-primary" disabled={!token.trim() || busy}>{busy ? "Creando sesión…" : "Crear sesión"}</button>
      </form>
      {(error || sessionError) && <div className="v3-notice error">{error || sessionError}</div>}
      <footer><ShieldCheck /> Los candidatos permanecen en tu sandbox hasta que los propongas al flujo editorial.</footer>
    </section>
  </main>;
}
