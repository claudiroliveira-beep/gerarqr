# app.py — SICT/SPG: QRs por avaliador + Formulário interno + SQLite + Área do Operador

import io
import re
import json
import zipfile
import unicodedata
import sqlite3
import gzip
import base64
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, quote_plus

import pandas as pd
import streamlit as st
import qrcode
from PIL import Image

# =========================
# Config da página
# =========================
st.set_page_config(page_title="SICT/SPG - Avaliações", page_icon="🧾", layout="wide")
st.title("🧾 SICT / SPG — Avaliações")

# =========================
# Constantes
# =========================
NBSP = "\xa0"
EVAL_DB = Path("avaliacoes.db")      # banco SQLite
DB_TABLE = "respostas"

EXPECTED_COLS = {
    "Aluno(a)": ["aluno", "aluno(a)", "aluna(o)"],
    "Orientador(a)": ["orientador", "orientador(a)"],
    "Áreas": ["áreas", "areas", "area", "área"],
    "Título": ["título", "titulo"],
    "AVALIADOR 1": ["avaliador 1", "avaliador1", "avaliador(a) 1"],
    "AVALIADOR 2": ["avaliador 2", "avaliador2", "avaliador(a) 2"],
    "Nº do Painel": ["nº do painel", "no do painel", "n do painel", "painel", "nº painel", "nº de painel"],
    "Subevento": ["subevento", "evento", "sub-evento", "evento/subevento"],
    "Dia": ["dia"],
    "Hora": ["hora"]
}

# =========================
# Utils
# =========================
def strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in text if not unicodedata.combining(ch))

def clean_cell(s: str) -> str:
    if s is None:
        return ""
    s = str(s).replace(NBSP, " ")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def col_key(name: str) -> str:
    return strip_accents(clean_cell(name).lower())

def find_column(target_keys, columns):
    colmap = {col_key(c): c for c in columns}
    for t in target_keys:
        k = col_key(t)
        if k in colmap:
            return colmap[k]
    for c in columns:
        if any(col_key(c).startswith(col_key(t)) for t in target_keys):
            return c
    return None

def ensure_expected_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Mapeia nomes flexíveis para o padrão e garante presença/ordem."""
    if df is None or df.empty:
        return pd.DataFrame(columns=list(EXPECTED_COLS.keys()))
    df = df.copy()
    df.columns = [clean_cell(c) for c in df.columns]
    rename_map = {}
    for std_name, options in EXPECTED_COLS.items():
        col = find_column(options, list(df.columns))
        if col:
            rename_map[col] = std_name
    df = df.rename(columns=rename_map)
    for k in EXPECTED_COLS.keys():
        if k not in df.columns:
            df[k] = ""
    df = df[list(EXPECTED_COLS.keys())].copy()
    for c in df.columns:
        df[c] = df[c].astype(str).map(clean_cell)
    return df

def tidy_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    # Forward-fill de campos hierárquicos
    ffill_cols = ["Subevento", "Áreas", "Dia", "Hora"]
    for c in ffill_cols:
        df[c] = df[c].replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    df[ffill_cols] = df[ffill_cols].ffill()

    # Painel sem ruído
    df["Nº do Painel"] = (
        df["Nº do Painel"].astype(str).str.replace(r"[^\dA-Za-z\-/. ]+", "", regex=True).str.strip()
    )

    # Normaliza Subevento (e.g., "XIV SICT", "XII SPG")
    def norm_event(x: str) -> str:
        s = strip_accents(x.upper().strip())
        s = re.sub(r"\s+", " ", s)
        m = re.search(r"\b([IVXLCDM]+)\s+(SICT|SPG)\b", s)
        if m:
            return f"{m.group(1)} {m.group(2)}"
        if "SICT" in s:
            return "SICT" if not re.search(r"\bSICT\b", s) else s
        if "SPG" in s:
            return "SPG" if not re.search(r"\bSPG\b", s) else s
        return s
    df["Subevento"] = df["Subevento"].map(norm_event)

    # Final
    for c in df.columns:
        df[c] = df[c].astype(str).map(clean_cell)
    df = df.drop_duplicates()
    return df

def build_mapping_by_evaluator(df: pd.DataFrame) -> dict:
    """Dicionário {avaliador: [trabalhos]}."""
    eval_map = {}
    def push(name, row):
        name = clean_cell(name)
        if not name:
            return
        eval_map.setdefault(name, []).append({
            "aluno": row.get("Aluno(a)", ""),
            "titulo": row.get("Título", ""),
            "orientador": row.get("Orientador(a)", ""),
            "area": row.get("Áreas", ""),
            "painel": row.get("Nº do Painel", ""),
            "subevento": row.get("Subevento", ""),
            "dia": row.get("Dia", ""),
            "hora": row.get("Hora", "")
        })
    for _, row in df.iterrows():
        push(row.get("AVALIADOR 1", ""), row)
        push(row.get("AVALIADOR 2", ""), row)
    for k in eval_map:
        eval_map[k] = sorted(
            eval_map[k], key=lambda r: (r["subevento"], r["dia"], r["hora"], r["painel"])
        )
    return eval_map

# Limites por item e total
ITEM_MAX = {
    "g1": 1.0,
    "g2": 1.0,
    "g3": 2.0,
    "g4": 3.0,
    "g5": 3.0,
}
TOTAL_MAX = 10.0

# =========================
# SQLite: init / save / load
# =========================
def init_db():
    """Cria a tabela se não existir + trava de duplicidade."""
    with sqlite3.connect(EVAL_DB) as conn:
        c = conn.cursor()
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                sheet TEXT,
                avaliador TEXT,
                aluno TEXT,
                orientador TEXT,
                titulo TEXT,
                painel TEXT,
                subevento TEXT,
                dia TEXT,
                hora TEXT,
                clareza_objetivos INTEGER,
                metodologia INTEGER,
                qualidade_resultados INTEGER,
                relevancia_originalidade INTEGER,
                apresentacao_defesa INTEGER,
                observacoes TEXT,
                UNIQUE (sheet, avaliador, painel, dia, hora)
            );
        """)
        conn.commit()

def save_evaluation_sqlite(record: dict) -> tuple[bool, str]:
    """Salva a avaliação no SQLite. Retorna (ok, msg)."""
    payload = (
        datetime.utcnow().isoformat(timespec="seconds") + "Z",
        record.get("Sheet", ""),
        record.get("Avaliador", ""),
        record.get("Aluno(a)", ""),
        record.get("Orientador(a)", ""),
        record.get("Título", ""),
        record.get("Nº do Painel", ""),
        record.get("Subevento", ""),
        record.get("Dia", ""),
        record.get("Hora", ""),
        int(record.get("Clareza_objetivos", 0.0)),
        int(record.get("Metodologia", 0.0)),
        int(record.get("Qualidade_resultados", 0.0)),
        int(record.get("Relevancia_originalidade", 0.0)),
        int(record.get("Apresentacao_defesa", 0.0)),
        record.get("Observacoes", "")
    )
    with sqlite3.connect(EVAL_DB) as conn:
        c = conn.cursor()
        try:
            c.execute(f"""
                INSERT INTO {DB_TABLE} (
                    created_at, sheet, avaliador, aluno, orientador, titulo, painel, subevento, dia, hora,
                    clareza_objetivos, metodologia, qualidade_resultados, relevancia_originalidade, apresentacao_defesa, observacoes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, payload)
            conn.commit()
            return True, "Avaliação salva com sucesso no banco de dados."
        except sqlite3.IntegrityError:
            return False, "Avaliação já existente para este avaliador nesse painel/dia/hora (trava de duplicidade)."
        except Exception as e:
            return False, f"Erro ao salvar no banco: {e}"

def load_evaluations_df() -> pd.DataFrame:
    """Carrega todas as avaliações do SQLite para DataFrame."""
    if not EVAL_DB.exists():
        return pd.DataFrame()
    with sqlite3.connect(EVAL_DB) as conn:
        df = pd.read_sql_query(f"SELECT * FROM {DB_TABLE} ORDER BY created_at DESC, id DESC", conn)
    return df

def export_evals_to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Respostas")
    buf.seek(0)
    return buf.getvalue()

def export_evals_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")

def is_operator(pin_input: str) -> bool:
    """Valida PIN do operador via st.secrets['OPERATOR_PIN'] (fallback '0000')."""
    conf = ""
    try:
        conf = st.secrets.get("OPERATOR_PIN", "")
    except Exception:
        conf = ""
    if not conf:
        conf = "0000"
    return str(pin_input).strip() == str(conf).strip()


# Quais colunas compõem a nota
CRITERIA_COLS = [
    "clareza_objetivos",
    "metodologia",
    "qualidade_resultados",
    "relevancia_originalidade",
    "apresentacao_defesa",
]

def with_total(df: pd.DataFrame) -> pd.DataFrame:
    """Adiciona coluna 'nota_total' = soma dos 5 critérios (1 casa)."""
    if df.empty:
        return df.copy()
    dff = df.copy()
    dff["nota_total"] = dff[CRITERIA_COLS].sum(axis=1).round(1)
    return dff

def aggregate_by_work(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega por trabalho (painel/dia/hora/subevento e identificadores).
    Média entre avaliadores (se só 1, é a própria nota).
    """
    if df.empty:
        return df.copy()

    keys = ["sheet","painel","dia","hora","subevento","titulo","aluno","orientador"]
    dff = with_total(df)
    g = dff.groupby(keys, dropna=False)

    out = g.agg(
        avaliadores=("avaliador", "nunique"),
        media_total=("nota_total", "mean"),
    ).reset_index()

    out["media_total"] = out["media_total"].round(1)
    return out


# =========================
# QR helpers (compactação/encoding)
# =========================
def encode_payload(payload: dict, mode: str = "json") -> str:
    """
    mode:
      - 'json'   : JSON compacto
      - 'gz+b64' : JSON compacto -> gzip -> base64 URL-safe
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if mode == "gz+b64":
        comp = gzip.compress(raw.encode("utf-8"))
        return base64.urlsafe_b64encode(comp).decode("ascii")
    return raw

def make_qr_image(payload_obj, box_size=5, border=1,
                  size_mode: str = "mini",   # 'normal' | 'mini' | 'ultra'
                  encoding: str = "gz+b64"   # 'json' | 'gz+b64'
                  ):
    """
    Gera QR com:
      - níveis:
          normal = tudo
          mini   = remove 'titulo'
          ultra  = mantém apenas painel/dia/hora/subevento/area + cabeçalho curto
      - encoding:
          'json'   -> JSON compacto
          'gz+b64' -> JSON compacto gz + base64
    """
    payload = payload_obj
    if isinstance(payload_obj, dict) and "trabalhos" in payload_obj:
        if size_mode == "mini":
            slim = [{k: v for k, v in r.items() if k != "titulo"} for r in payload_obj["trabalhos"]]
            payload = {**payload_obj, "trabalhos": slim}
        elif size_mode == "ultra":
            keep = {"painel", "dia", "hora", "subevento", "area"}
            slim = [{k: r.get(k, "") for k in keep} for r in payload_obj["trabalhos"]]
            payload = {"a": payload_obj.get("avaliador", ""), "n": payload_obj.get("quantidade_trabalhos", len(slim)), "t": slim}

    txt = encode_payload(payload, encoding)

    def try_build(txt_, err_level):
        qr = qrcode.QRCode(version=None, error_correction=err_level, box_size=box_size, border=border)
        qr.add_data(txt_)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")

    for lvl in [qrcode.constants.ERROR_CORRECT_Q,
                qrcode.constants.ERROR_CORRECT_M,
                qrcode.constants.ERROR_CORRECT_L]:
        try:
            return try_build(txt, lvl)
        except Exception:
            continue

    if size_mode != "ultra" or encoding != "gz+b64":
        return make_qr_image(payload_obj, box_size=box_size, border=border, size_mode="ultra", encoding="gz+b64")
    raise RuntimeError("QR ainda excede a capacidade mesmo em ultra + gzip.")

def badge_evento(subeventos: list[str]) -> str:
    if not subeventos:
        return ""
    pills = []
    for s in subeventos:
        color = "#2563EB" if "SICT" in s.upper() else "#047857" if "SPG" in s.upper() else "#374151"
        pills.append(
            f"""<span style="display:inline-block;padding:4px 10px;margin:0 6px 6px 0;
            border-radius:9999px;background:{color};color:white;font-size:12px;font-weight:600;">{s}</span>"""
        )
    return "<div>" + "".join(pills) + "</div>"

def make_internal_link(params: dict) -> str:
    return "?" + urlencode(params, quote_via=quote_plus)

# =========================
# Entrada Excel
# =========================
st.sidebar.header("📄 Planilha do Cronograma")
uploaded = st.sidebar.file_uploader("Envie o arquivo .xlsx", type=["xlsx"])
df_dict = {}
if uploaded:
    xls = pd.ExcelFile(uploaded)
    df_dict = {name: pd.read_excel(xls, sheet_name=name) for name in xls.sheet_names}
else:
    for p in [Path("SPG.xlsx"), Path("SICT.xlsx"), Path("cronograma_SICT_SPG.xlsx"), Path("cronograma.xlsx")]:
        if p.exists():
            xls = pd.ExcelFile(p)
            df_dict = {name: pd.read_excel(xls, sheet_name=name) for name in xls.sheet_names}
            break

if not df_dict:
    st.info("Envie uma planilha .xlsx ou inclua `cronograma_SICT_SPG.xlsx` no repositório.")
    st.stop()

sheet_name = st.sidebar.selectbox("Aba", list(df_dict.keys()), index=0)
raw_df = df_dict[sheet_name]
df = tidy_dataframe(ensure_expected_columns(raw_df))

# Inicia/garante banco
init_db()

# =========================
# Query params (roteamento simples)
# =========================
qp = st.query_params
def _qp_get(key, default=""):
    val = qp.get(key, default)
    if isinstance(val, list):
        return val[0] if val else default
    return val
acao = _qp_get("acao", "")
qp_sheet = _qp_get("sheet", "")
qp_avaliador = _qp_get("avaliador", "")
qp_row = _qp_get("row", "")

# =========================
# Mapa por avaliador + UI principal
# =========================
eval_map = build_mapping_by_evaluator(df)
all_evals = sorted(eval_map.keys())

st.subheader(f"🎯 Trabalhos por Avaliador — Aba: **{sheet_name}**")
c1, c2, c3 = st.columns([2,1.2,1.2])
with c1:
    selected_eval = st.selectbox("Avaliador(a)", [""] + all_evals,
                                 index=(all_evals.index(qp_avaliador)+1 if qp_avaliador in all_evals else 0))
with c2:
    qr_mode = st.selectbox("Tamanho do conteúdo do QR",
                           ["normal", "mini", "ultra"], index=1,
                           help="mini remove títulos; ultra mantém apenas painel/dia/hora/subevento/área")
with c3:
    qr_enc = st.selectbox("Codificação",
                          ["json", "gz+b64"], index=1,
                          help="gz+b64 deixa o QR menor (precisa de decodificação no leitor)")

if selected_eval:
    # Tabela de trabalhos do avaliador
    df_show = pd.DataFrame(eval_map[selected_eval])
    df_show_ren = df_show.rename(columns={
        "aluno": "Aluno(a)", "titulo": "Título", "orientador": "Orientador(a)",
        "area": "Área", "painel": "Nº do Painel", "subevento": "Subevento",
        "dia": "Dia", "hora": "Hora"
    })
    df_show_ren.reset_index(inplace=True)
    df_show_ren.rename(columns={"index": "ID"}, inplace=True)

    # Link interno "Avaliar"
    links = []
    for _, r in df_show_ren.iterrows():
        link = make_internal_link({
            "acao": "avaliar",
            "sheet": sheet_name,
            "avaliador": selected_eval,
            "row": int(r["ID"])
        })
        links.append(link)
    df_show_ren["Avaliar"] = links

    cols_final = ["Aluno(a)", "Título", "Orientador(a)", "Nº do Painel", "Área", "Subevento", "Dia", "Hora", "Avaliar"]
    st.data_editor(
        df_show_ren[cols_final],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Avaliar": st.column_config.LinkColumn("Avaliar", display_text="Avaliar")
        }
    )

    # QR do avaliador (compacto)
    payload = {
        "avaliador": selected_eval,
        "quantidade_trabalhos": len(eval_map[selected_eval]),
        "trabalhos": eval_map[selected_eval]
    }
    img = make_qr_image(payload, box_size=5, border=1, size_mode=qr_mode, encoding=qr_enc)
    st.image(img, caption=f"QR — {selected_eval}")
    buf = io.BytesIO(); img.save(buf, format="PNG")
    st.download_button("⬇️ Baixar QR", buf.getvalue(), f"QR_{selected_eval}.png", "image/png")

    # ZIP (todos, em modo ultra + gz+b64)
    if st.checkbox("Gerar ZIP com todos os QRs (ultra + gz+b64)"):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in all_evals:
                payload_n = {
                    "avaliador": name,
                    "quantidade_trabalhos": len(eval_map[name]),
                    "trabalhos": eval_map[name]
                }
                img_n = make_qr_image(payload_n, box_size=5, border=1, size_mode="ultra", encoding="gz+b64")
                b = io.BytesIO(); img_n.save(b, format="PNG")
                zf.writestr(f"QR_{name}.png", b.getvalue())
        st.download_button("⬇️ Baixar ZIP (ultra)", zip_buffer.getvalue(),
                           f"QRs_{sheet_name}.zip", "application/zip")

st.divider()

# =========================
# FORMULÁRIO INTERNO (?acao=avaliar)
# =========================
if acao == "avaliar" and qp_sheet == sheet_name and qp_avaliador:
    st.subheader("📝 Formulário de Avaliação (interno)")

    if qp_avaliador in eval_map:
        works = eval_map[qp_avaliador]
        try:
            idx = int(qp_row)
            work = works[idx]
        except Exception:
            st.error("Não foi possível localizar o trabalho. Volte e clique novamente em 'Avaliar'.")
            work = None

        if work:
            # --- FORM (somente campos + submit; sem download_button aqui dentro) ---
            with st.form(key=f"form_{qp_avaliador}_{idx}"):
                c1, c2 = st.columns([2, 2])
                with c1:
                    st.text_input("Título", value=work["titulo"], disabled=True)
                    st.text_input("Autor(a)", value=work["aluno"], disabled=True)
                    st.text_input("Orientador(a)", value=work["orientador"], disabled=True)
                    st.text_input("Avaliador(a)", value=qp_avaliador, disabled=True)
                with c2:
                    st.text_input("Subevento", value=work["subevento"], disabled=True)
                    st.text_input("Nº do Painel", value=work["painel"], disabled=True)
                    st.text_input("Dia", value=work["dia"], disabled=True)
                    st.text_input("Hora", value=work["hora"], disabled=True)

                st.markdown("**Avaliação — total máximo 10 pontos (cada item em passos de 0,1)**")
                st.caption(
                    f"Limites por item: Q1. ≤ {ITEM_MAX['g1']:.1f}, Q2. ≤ {ITEM_MAX['g2']:.1f}, "
                    f"Q3. ≤ {ITEM_MAX['g3']:.1f}, Q4. ≤ {ITEM_MAX['g4']:.1f}, Q5. ≤ {ITEM_MAX['g5']:.1f}."
                )
                
                # Entradas com cap por item e passo 0.1
                g1 = st.number_input(
                    "Q1. A formatação do pôster está de fácil leitura e houve a inclusão de ferramentas adequadas para exposição do tema?",
                    min_value=0.0, max_value=float(ITEM_MAX["g1"]), value=0.0, step=0.1, format="%.1f"
                )
                g2 = st.number_input(
                    "Q2. A organização do material apresentado segue uma ordem de fácil compreensão?",
                    min_value=0.0, max_value=float(ITEM_MAX["g2"]), value=0.0, step=0.1, format="%.1f"
                )
                g3 = st.number_input(
                    "Q3. O(a) bolsista/voluntário(a) respondeu às perguntas da banca adequadamente?",
                    min_value=0.0, max_value=float(ITEM_MAX["g3"]), value=0.0, step=0.1, format="%.1f"
                )
                g4 = st.number_input(
                    "Q4. O(a) bolsista/voluntário(a) apresentou domínio do tema?",
                    min_value=0.0, max_value=float(ITEM_MAX["g4"]), value=0.0, step=0.1, format="%.1f"
                )
                g5 = st.number_input(
                    "Q5. Qualidade dos resultados",
                    min_value=0.0, max_value=float(ITEM_MAX["g5"]), value=0.0, step=0.1, format="%.1f"
                )

                obs = st.text_area("Observações (opcional)", "")

                # Cálculo e validação
                total = round(g1 + g2 + g3 + g4 + g5, 1)
                restante = round(TOTAL_MAX - total, 1)
                st.info(f"**Total atual:** {total:.1f} / {TOTAL_MAX:.1f}  •  **Pontos restantes:** {max(restante, 0):.1f}")
                
                # Regra: total não pode ultrapassar 10 (mesmo com caps individuais)
                excede_total = total > TOTAL_MAX
                if excede_total:
                    st.error("A soma dos itens ultrapassa 10. Ajuste as notas antes de salvar.")
                
                submitted = st.form_submit_button("Salvar avaliação", disabled=excede_total)
                
                if submitted:
                    # Validação definitiva no servidor (não salva se inválido)
                    if total > TOTAL_MAX:
                        st.error("Não foi possível salvar: a soma dos itens ultrapassa 10.")
                        # Não salva nada
                    else:
                        record = {
                            "Sheet": sheet_name,
                            "Avaliador": qp_avaliador,
                            "Aluno(a)": work["aluno"],
                            "Orientador(a)": work["orientador"],
                            "Título": work["titulo"],
                            "Nº do Painel": work["painel"],
                            "Subevento": work["subevento"],
                            "Dia": work["dia"],
                            "Hora": work["hora"],
                            "Clareza_objetivos": float(g1),
                            "Metodologia": float(g2),
                            "Qualidade_resultados": float(g3),
                            "Relevancia_originalidade": float(g4),
                            "Apresentacao_defesa": float(g5),
                            "Observacoes": obs,
                        }
                
                        ok, msg = save_evaluation_sqlite(record)
                        if ok:
                            st.success(f"✅ {msg} (Total: {total:.1f}/10)")
                        else:
                            st.warning("⚠️ " + msg)

            
# =========================
# ÁREA DO OPERADOR (PIN) — Filtros, Tabela, Exportação
# =========================
st.divider()
with st.sidebar.expander("🔐 Área do Operador", expanded=False):
    pin_try = st.text_input("PIN do operador", type="password")
    op_go = st.button("Entrar")

if 'operator_mode' not in st.session_state:
    st.session_state['operator_mode'] = False
if op_go:
    st.session_state['operator_mode'] = is_operator(pin_try)

if st.session_state['operator_mode']:
    st.subheader("📊 Painel do Operador — Resumo & Exportação")

    df_evals = load_evaluations_df()
    if df_evals.empty:
        st.info("Sem avaliações registradas ainda.")
    else:
        # Filtros
        left, mid, right, right2 = st.columns([1.5, 1.5, 1, 1])
        with left:
            sub_opts = ["(Todos)"] + sorted([x for x in df_evals["subevento"].dropna().unique().tolist() if x])
            sub_sel = st.selectbox("Subevento", options=sub_opts, index=0)
        with mid:
            aval_opts = ["(Todos)"] + sorted([x for x in df_evals["avaliador"].dropna().unique().tolist() if x])
            aval_sel = st.selectbox("Avaliador(a)", options=aval_opts, index=0)
        with right:
            dia_opts = ["(Todos)"] + sorted([x for x in df_evals["dia"].dropna().unique().tolist() if x])
            dia_sel = st.selectbox("Dia", options=dia_opts, index=0)
        with right2:
            hora_opts = ["(Todos)"] + sorted([x for x in df_evals["hora"].dropna().unique().tolist() if x])
            hora_sel = st.selectbox("Hora", options=hora_opts, index=0)
        
        # Aplica filtros
        dff = df_evals.copy()
        if sub_sel != "(Todos)":
            dff = dff[dff["subevento"] == sub_sel]
        if aval_sel != "(Todos)":
            dff = dff[dff["avaliador"] == aval_sel]
        if dia_sel != "(Todos)":
            dff = dff[dff["dia"] == dia_sel]
        if hora_sel != "(Todos)":
            dff = dff[dff["hora"] == hora_sel]
        
        # Calcula nota total nas respostas filtradas
        dff = with_total(dff)
        
        # Métricas (opcional; mantenho porque são úteis e não duplicam tabelas)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total de avaliações", len(dff))
        m2.metric("Avaliadores únicos", dff["avaliador"].nunique())
        m3.metric("Trabalhos únicos", dff[["painel","dia","hora","subevento"]].drop_duplicates().shape[0])
        m4.metric("Última atualização (UTC)", dff["created_at"].max() if not dff.empty else "—")
        
        # ---- SOMENTE AS TABS ----
        tab_resp, tab_media = st.tabs(["Respostas", "Médias por trabalho"])
        
        with tab_resp:
            st.dataframe(
                dff[[
                    "created_at","sheet","avaliador","aluno","orientador","titulo",
                    "painel","subevento","dia","hora",
                    "clareza_objetivos","metodologia","qualidade_resultados","relevancia_originalidade","apresentacao_defesa",
                    "nota_total","observacoes","id"
                ]].rename(columns={
                    "created_at":"Quando(UTC)","sheet":"Aba","avaliador":"Avaliador",
                    "aluno":"Aluno(a)","orientador":"Orientador(a)","titulo":"Título",
                    "painel":"Nº Painel","subevento":"Subevento","dia":"Dia","hora":"Hora",
                    "clareza_objetivos":"Clareza","metodologia":"Metodologia","qualidade_resultados":"Resultados",
                    "relevancia_originalidade":"Relevância","apresentacao_defesa":"Apresentação",
                    "nota_total":"Nota total","observacoes":"Observações","id":"ID"
                }),
                use_container_width=True,
                height=420
            )
        
            c1, c2 = st.columns([1,1])
            with c1:
                if st.button("Gerar Excel (Respostas)", key="gen_excel_respostas"):
                    st.session_state["op_excel_bytes_resp"] = export_evals_to_excel_bytes(dff)
                    st.session_state["op_excel_ready_resp"] = True
            with c2:
                if st.button("Gerar CSV (Respostas)", key="gen_csv_respostas"):
                    st.session_state["op_csv_bytes_resp"] = export_evals_to_csv_bytes(dff)
                    st.session_state["op_csv_ready_resp"] = True
        
            d1, d2 = st.columns([1,1])
            with d1:
                if st.session_state.get("op_excel_ready_resp") and st.session_state.get("op_excel_bytes_resp"):
                    st.download_button(
                        "⬇️ Baixar Excel (Respostas)",
                        data=st.session_state["op_excel_bytes_resp"],
                        file_name="avaliacoes_respostas.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="dl_excel_respostas"
                    )
            with d2:
                if st.session_state.get("op_csv_ready_resp") and st.session_state.get("op_csv_bytes_resp"):
                    st.download_button(
                        "⬇️ Baixar CSV (Respostas)",
                        data=st.session_state["op_csv_bytes_resp"],
                        file_name="avaliacoes_respostas.csv",
                        mime="text/csv",
                        key="dl_csv_respostas"
                    )
        
        with tab_media:
            agg = aggregate_by_work(dff)
        
            mm1, mm2 = st.columns(2)
            mm1.metric("Trabalhos com ≥ 2 avaliadores", int((agg["avaliadores"] >= 2).sum()) if not agg.empty else 0)
            mm2.metric("Média geral (todos os trabalhos)", agg["media_total"].mean().round(2) if not agg.empty else "—")
        
            st.dataframe(
                agg[[
                    "sheet","titulo","aluno","orientador",
                    "painel","subevento","dia","hora",
                    "avaliadores","media_total"
                ]].rename(columns={
                    "sheet":"Aba","titulo":"Título","aluno":"Aluno(a)","orientador":"Orientador(a)",
                    "painel":"Nº Painel","subevento":"Subevento","dia":"Dia","hora":"Hora",
                    "avaliadores":"# Avaliadores","media_total":"Média final"
                }),
                use_container_width=True,
                height=420
            )
        
            c3, c4 = st.columns([1,1])
            with c3:
                if st.button("Gerar Excel (Médias)", key="gen_excel_medias"):
                    st.session_state["op_excel_bytes_medias"] = export_evals_to_excel_bytes(agg)
                    st.session_state["op_excel_ready_medias"] = True
            with c4:
                if st.button("Gerar CSV (Médias)", key="gen_csv_medias"):
                    st.session_state["op_csv_bytes_medias"] = export_evals_to_csv_bytes(agg)
                    st.session_state["op_csv_ready_medias"] = True
        
            d3, d4 = st.columns([1,1])
            with d3:
                if st.session_state.get("op_excel_ready_medias") and st.session_state.get("op_excel_bytes_medias"):
                    st.download_button(
                        "⬇️ Baixar Excel (Médias)",
                        data=st.session_state["op_excel_bytes_medias"],
                        file_name="avaliacoes_medias.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="dl_excel_medias"
                    )
            with d4:
                if st.session_state.get("op_csv_ready_medias") and st.session_state.get("op_csv_bytes_medias"):
                    st.download_button(
                        "⬇️ Baixar CSV (Médias)",
                        data=st.session_state["op_csv_bytes_medias"],
                        file_name="avaliacoes_medias.csv",
                        mime="text/csv",
                        key="dl_csv_medias"
                    )



     
