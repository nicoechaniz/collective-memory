#!/usr/bin/env python3
"""Cola de jobs del Discovery Playground (F14b).

Jobs = campañas de discovery de la mesa chica, ejecutadas como SUBPROCESO
(`discover.py run --db <sandbox> --owner <user> ...`) por un worker supervisor
único (FIFO). Subproceso y no thread porque: (a) timeout real por terminate/kill;
(b) los sys.exit() del motor se vuelven exit codes, no SystemExit en el worker;
(c) aislamiento de memoria del proceso servidor.

GPU: un solo job a la vez; el job descarga bge-m3 al terminar (run_campaign lo
hace en su cierre) y acá se registra la VRAM al inicio/fin como telemetría.
"""
import json
import os
import subprocess
import sys
import threading
import time

import discover
from discover import now_iso

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import CODE_HOME  # noqa: E402

VENV_PY = os.environ.get("MAPA_PYTHON", sys.executable)
DISCOVER_CLI = os.path.join(CODE_HOME, "discover.py")
VALID_OPERATORS = ("latent_bridge", "cluster_frontier", "outlier", "tension", "analogy", "freshness_negative")

MAX_QUEUED_PER_USER = 2
MAX_JOBS_PER_USER_DAY = 10
LIMIT_MAX = 30
JOB_TIMEOUT = 1800  # 30 min duros
TAIL_CAP = 8000     # caps de stdout/stderr persistidos

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY,
  owner TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'campaign',
  params_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  result_json TEXT,
  error TEXT,
  pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def open_jobs():
    con = discover.open_disc(discover.PG_DB, create=True)
    con.executescript(JOBS_SCHEMA)
    return con


def gpu_mem_mib():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


TASK_MAX = 2000  # cap del prompt de usuario (modo agente)


def validate_params(params):
    """Devuelve (params_normalizados, None) o (None, error_str).

    Unión ESTRICTAMENTE exclusiva: exactamente uno de `task` (modo agente) o
    `operators` (modo barrido). Ambos vacíos o ambos presentes → error."""
    provider = params.get("provider") or "default"
    if not isinstance(provider, str) or len(provider) > 64:
        return None, "provider inválido"
    task = params.get("task")
    has_task = isinstance(task, str) and task.strip()
    has_ops = bool(params.get("operators"))
    if has_task and has_ops:
        return None, "task y operators son mutuamente excluyentes: mandá uno solo"
    if not has_task and not has_ops:
        return None, "falta task (modo agente) u operators (modo barrido)"
    if has_task:
        if len(task) > TASK_MAX:
            return None, f"task demasiado largo (max {TASK_MAX} chars)"
        return {"mode": "agent", "task": task.strip(), "provider": provider}, None
    # modo operadores
    ops = params.get("operators")
    if ops == "all":
        ops = list(VALID_OPERATORS)
    if not isinstance(ops, list) or not ops or any(o not in VALID_OPERATORS for o in ops):
        return None, f"operators inválidos (válidos: {', '.join(VALID_OPERATORS)} o 'all')"
    try:
        limit = min(int(params.get("limit", 12)), LIMIT_MAX)
    except (TypeError, ValueError):
        return None, "limit inválido"
    if limit < 1:
        return None, "limit inválido"
    projects = params.get("projects") or []
    if not isinstance(projects, list) or any(not isinstance(p, str) or len(p) > 128 for p in projects):
        return None, "projects inválidos"
    return {"mode": "operators", "operators": ops, "limit": limit,
            "projects": projects[:10], "provider": provider}, None


def enqueue(owner, params):
    """Encola un job. Devuelve (job_id, None) o (None, (código_http, error))."""
    norm, err = validate_params(params)
    if err:
        return None, (400, err)
    con = open_jobs()
    try:
        # check+insert bajo UNA transacción inmediata: dos requests concurrentes del
        # mismo token no pueden ambas leer el mismo contador y colarse.
        con.execute("BEGIN IMMEDIATE")
        active = con.execute("SELECT count(*) FROM jobs WHERE owner=? AND status IN ('queued','running')",
                             (owner,)).fetchone()[0]
        if active >= MAX_QUEUED_PER_USER:
            con.rollback()
            return None, (429, f"máximo {MAX_QUEUED_PER_USER} jobs activos por usuario")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        today = con.execute("SELECT count(*) FROM jobs WHERE owner=? AND created_at LIKE ?",
                            (owner, day + "%")).fetchone()[0]
        if today >= MAX_JOBS_PER_USER_DAY:
            con.rollback()
            return None, (429, f"máximo {MAX_JOBS_PER_USER_DAY} jobs por día por usuario")
        con.execute("INSERT INTO jobs(owner, params_json, status, created_at) VALUES(?,?,?,?)",
                    (owner, json.dumps(norm, ensure_ascii=False), "queued", now_iso()))
        jid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit()
        return jid, None
    finally:
        con.close()


def cancel_user_jobs(owner):
    """Revocación de usuario: SOLO marca los jobs cancelled bajo BEGIN IMMEDIATE. NO mata
    procesos — este CLI corre en un proceso SEPARADO del worker, y matar por un pid leído
    de la DB es TOCTOU (el pid pudo reusarse). El WORKER, que tiene el handle Popen real de
    su hijo, detecta 'cancelled' en su poll loop y lo mata sin carrera (padre + zombie).
    Un job 'queued' cancelado nunca se levanta (el claim del worker filtra por status)."""
    con = open_jobs()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE jobs SET status='cancelled', "
                    "error='cancelado por revocación de usuario', finished_at=? "
                    "WHERE owner=? AND status IN ('queued','running')", (now_iso(), owner))
        con.commit()
    finally:
        con.close()


def _run_job(con, job):
    params = json.loads(job["params_json"])
    base = [VENV_PY, DISCOVER_CLI, "run", "--db", discover.PG_DB,
            "--owner", job["owner"], "--provider", params["provider"]]
    if params.get("mode") == "agent":
        # modo agente: sin --operators ni --campaign (el adaptador genera el nombre)
        cmd = base + ["--task", params["task"]]
    else:
        cmd = base + ["--campaign", f"lab-{job['owner']}-{job['id']}",
                      "--operators", ",".join(params["operators"]),
                      "--limit", str(params["limit"])]
        for p in params["projects"]:
            cmd += ["--project", p]
    vram0 = gpu_mem_mib()
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, cwd=CODE_HOME)
    # Reclamar el pid SOLO si el job sigue 'running': si fue cancelado en la ventana entre
    # el claim y el Popen, perdemos el CAS y matamos al hijo recién nacido de inmediato.
    con.execute("BEGIN IMMEDIATE")
    claimed = con.execute("UPDATE jobs SET pid=? WHERE id=? AND status='running'",
                          (proc.pid, job["id"])).rowcount == 1
    con.commit()
    if not claimed:
        _kill_child(proc)  # cancelado en la ventana claim↔Popen
        return
    # Poll loop: el worker (padre, con el handle Popen real) espera en tramos cortos y en
    # cada uno chequea (a) timeout global y (b) revocación (status='cancelled' en la DB).
    # Matar vía el objeto Popen no tiene TOCTOU: mientras no hagamos wait(), un hijo muerto
    # queda zombie y su pid no se reutiliza.
    deadline = time.time() + JOB_TIMEOUT
    status, out, errout = None, "", ""
    while True:
        try:
            out, errout = proc.communicate(timeout=5)  # retomar tras timeout no pierde output
            status = "done" if proc.returncode == 0 else "error"
            break
        except subprocess.TimeoutExpired:
            if con.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()["status"] == "cancelled":
                _kill_child(proc)
                return  # ya está 'cancelled'; no lo pisamos
            if time.time() >= deadline:
                _kill_child(proc)
                out, errout = proc.communicate()
                status = "timeout"
                break
    result = {"stdout_tail": (out or "")[-TAIL_CAP:], "returncode": proc.returncode,
              "vram_mib_start": vram0, "vram_mib_end": gpu_mem_mib()}
    # Condicional por (running, pid propio): si una revocación lo marcó 'cancelled' en la
    # ventana justa, NO lo pisamos con done/error/timeout.
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE jobs SET status=?, finished_at=?, result_json=?, error=?, pid=NULL "
                "WHERE id=? AND status='running' AND pid=?",
                (status, now_iso(), json.dumps(result, ensure_ascii=False),
                 (errout or "")[-TAIL_CAP:] if status != "done" else None, job["id"], proc.pid))
    con.commit()


def _kill_child(proc):
    """Mata al hijo vía su handle Popen (padre → sin TOCTOU de pid-reuse)."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class Worker(threading.Thread):
    """Supervisor único FIFO. Captura BaseException: un job podrido jamás mata la cola."""

    def __init__(self):
        super().__init__(daemon=True, name="pg-worker")
        self.stop_event = threading.Event()

    def run(self):
        # Jobs 'running' huérfanos de un arranque anterior → error (el proceso ya no existe).
        con = open_jobs()
        con.execute("UPDATE jobs SET status='error', error='huérfano: el servicio se reinició', "
                    "finished_at=? WHERE status='running'", (now_iso(),))
        con.commit()
        while not self.stop_event.is_set():
            try:
                row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if not row:
                    self.stop_event.wait(2)
                    continue
                con.execute("BEGIN IMMEDIATE")
                claimed = con.execute("UPDATE jobs SET status='running', started_at=? "
                                      "WHERE id=? AND status='queued'", (now_iso(), row["id"])).rowcount == 1
                con.commit()
                if not claimed:
                    continue
                _run_job(con, dict(row))
            except BaseException as e:  # noqa: BLE001 — el worker sobrevive a todo
                try:
                    con.rollback()
                    con.execute("UPDATE jobs SET status='error', error=?, finished_at=? "
                                "WHERE status='running'", (f"worker: {e}", now_iso()))
                    con.commit()
                except Exception:
                    pass
                time.sleep(2)
