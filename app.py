# app.py — Leitura Excel, QRs e Formulário interno de Avaliação (salva em Excel)

import io
import re
import json
import zipfile
import unicodedata
from pathlib import Path
from urllib.parse import urlencode, quote_plus

import pandas as pd
import streamlit as st
import qrcode
from PIL import Image

# =========================
# Config
# =========================
st.set_page_config(page_title="SICT/SPG — QRs & Avaliações", page_icon="🧾", layout="wide")
st.title("🧾 SICT / SPG — Avaliações e QRs (Excel)")

NBSP = "\xa0"
EVAL_FILE = Path("avaliacoes.xlsx")  # arquivo onde as respostas serão salvas
EVAL_SHEET = "Respostas"

EXPECTED_COLS = {
    "Aluno(a)": ["aluno", "aluno(a)", "aluna(o)"],
    "Orientador(a)": ["orientador", "orientador(a)"],
    "Áreas": ["áreas", "areas", "area", "área"],
    "Título": ["título", "titulo"],
    "AVALIADOR 1": ["avaliador 1", "avaliador1", "avaliador(a) 1"],
    "AVALIADOR 2": ["avaliador 2", "avaliador2", "avaliador(a) 2"],
    "Nº do Painel": ["nº do painel", "no do painel", "n do painel", "painel", "nº painel", "nº de painel"],
    "Subevento": ["subevento", "evento"],
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
    # forward fill de campos hierárquicos (quando vieram de células mescladas)
    ffill_cols = ["Subevento", "Áreas", "Dia", "Hora"]
    for c in ffill_cols:
        df[c] = df[c].replace({"": pd.NA, "nan": pd.NA})
    df[ffill_cols] = df[ffill_cols].ffill()

    # Padroniza Nº do Painel
    df["Nº do Painel"] = (
        df["Nº do Painel"]
        .astype(str).str.replace(r"[^\dA-Za-z\-/. ]+", "", regex=True).str.strip()
    )

    # Normaliza Subevento
    def norm_event(x: str) -> str:
        s = strip_accents(x.upper().strip())
        m = re.search(r"\b([IVXLCDM]+)\s+(SICT|SPG)\b", s)
        if m:
            return f"{m.group(1)} {m.group(2)}"
        if "SICT" in s: return "SICT"
        if "SPG" in s: return "SPG"
        return s
    df["Subevento"] = df["Subevento"].map(norm_event)

    # Final
    for c in df.columns:
        df[c] = df[c].astype(str).map(clean_cell)
    return df.drop_duplicates()

def build_mapping_by_evaluator(df: pd.DataFrame) -> dict:
    eval_map = {}
    def push(name, row):
        name = clean_cell(name)
        if not name: return
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
        eval_map[k] = sorted(eval_map[k], key=lambda r: (r["subevento"], r["dia"], r["hora"], r["painel"]))
    return eval_map

# QR robusto e compacto
def make_qr_image(payload_obj, box_size=6, border=2, mini=False):
    def to_text(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    def try_build(txt, err_level):
        qr = qrcode.QRCode(
            version=None,
            error_correction=err_level,
            box_size=box_size,
            border=border
        )
        qr.add_data(txt)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")
    payload = payload_obj
    if mini and "trabalhos" in payload:
        slim = [{k:v for k,v in r.items() if k!="titulo"} for r in payload["trabalhos"]]
        payload = {**payload, "trabalhos": slim}
    txt = to_text(payload)
    for lvl in [qrcode.constants.ERROR_CORRECT_Q,
                qrcode.constants.ERROR_CORRECT_M,
                qrcode.constants.ERROR_CORRECT_L]:
        try:
            return try_build(txt, lvl)
        except Exception:
            continue
    if not mini:
        return make_qr_image(payload_obj, box_size, border, mini=True)
    raise RuntimeError("QR muito grande mesmo no modo compacto.")

def badge_evento(subeventos: list[str]) -> str:
    if not subeventos: return ""
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

# =========================
# Roteamento simples via query params
# =========================
qp = st.query_params
acao = qp.get("acao", [""])[0] if isinstance(qp.get("acao"), list) else qp.get("acao", "")
qp_sheet = qp.get("sheet", [""])[0] if isinstance(qp.get("sheet"), list) else qp.get("sheet", "")
qp_avaliador = qp.get("avaliador", [""])[0] if isinstance(qp.get("avaliador"), list) else qp.get("avaliador", "")
qp_row = qp.get("row", [""])[0] if isinstance(qp.get("row"), list) else qp.get("row", "")

# =========================
# Mapa por avaliador + UI principal
# =========================
eval_map = build_mapping_by_evaluator(df)
all_evals = sorted(eval_map.keys())

st.subheader(f"🎯 Trabalhos por Avaliador — Aba: **{sheet_name}**")
c1, c2, c3 = st.columns([2,1,1])
with c1:
    selected_eval = st.selectbox("Avaliador(a)", [""] + all_evals, index=(all_evals.index(qp_avaliador)+1 if qp_avaliador in all_evals else 0))
with c2:
    mini_mode = st.checkbox("QR mini (sem título)", value=False)
with c3:
    show_ids = st.checkbox("Mostrar ID interno da linha", value=False, help="Útil para auditoria do link 'Avaliar'.")

if selected_eval:
    # tabela de trabalhos do avaliador
    df_show = pd.DataFrame(eval_map[selected_eval])
    # acrescenta Orientador, se precisar (já incluímos no map)
    df_show_ren = df_show.rename(columns={
        "aluno": "Aluno(a)", "titulo": "Título", "orientador": "Orientador(a)",
        "area": "Área", "painel": "Nº do Painel", "subevento": "Subevento",
        "dia": "Dia", "hora": "Hora"
    })
    # adiciona índice (para "row")
    df_show_ren.reset_index(inplace=True)
    df_show_ren.rename(columns={"index": "ID"}, inplace=True)

    # cria col. Avaliar (link interno)
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

    # colunas finais
    cols_final = ["Aluno(a)", "Título", "Orientador(a)", "Nº do Painel", "Área", "Subevento", "Dia", "Hora", "Avaliar"]
    if show_ids:
        cols_final = ["ID"] + cols_final

    # exibição com link clicável
    st.data_editor(
        df_show_ren[cols_final],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Avaliar": st.column_config.LinkColumn("Avaliar", display_text="Avaliar")
        }
    )

    # QR do avaliador
    payload = {"avaliador": selected_eval, "quantidade_trabalhos": len(eval_map[selected_eval]), "trabalhos": eval_map[selected_eval]}
    img = make_qr_image(payload, box_size=6, border=2, mini=mini_mode)
    st.image(img, caption=f"QR — {selected_eval}")
    buf = io.BytesIO(); img.save(buf, format="PNG")
    st.download_button("⬇️ Baixar QR", buf.getvalue(), f"QR_{selected_eval}.png", "image/png")

    # ZIP (todos)
    if st.checkbox("Gerar ZIP com todos os QRs (mini)"):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in all_evals:
                payload_n = {"avaliador": name, "quantidade_trabalhos": len(eval_map[name]), "trabalhos": eval_map[name]}
                img_n = make_qr_image(payload_n, box_size=6, border=2, mini=True)
                b = io.BytesIO(); img_n.save(b, format="PNG")
                zf.writestr(f"QR_{name}.png", b.getvalue())
        st.download_button("⬇️ Baixar ZIP (mini)", zip_buffer.getvalue(), f"QRs_{sheet_name}.zip", "application/zip")

st.divider()

# =========================
# FORMULÁRIO INTERNO (rota ?acao=avaliar)
# =========================
if acao == "avaliar" and qp_sheet == sheet_name and qp_avaliador:
    st.subheader("📝 Formulário de Avaliação (interno)")
    # recupera a linha do avaliador selecionado
    if qp_avaliador in eval_map:
        works = eval_map[qp_avaliador]
        try:
            idx = int(qp_row)
            work = works[idx]
        except Exception:
            st.error("Não foi possível localizar o trabalho. Volte e clique novamente em 'Avaliar'.")
            work = None
        if work:
            with st.form(key=f"form_{qp_avaliador}_{idx}"):
                c1, c2 = st.columns([2,2])
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

                st.markdown("**Avaliação (1 = insuficiente … 5 = excelente)**")
                g1 = st.slider("1) Clareza dos objetivos", 1, 5, 3)
                g2 = st.slider("2) Metodologia adequada", 1, 5, 3)
                g3 = st.slider("3) Qualidade dos resultados", 1, 5, 3)
                g4 = st.slider("4) Relevância / Originalidade", 1, 5, 3)
                g5 = st.slider("5) Apresentação / Defesa", 1, 5, 3)
                obs = st.text_area("Observações (opcional)", "")

                submitted = st.form_submit_button("Salvar avaliação")
                if submitted:
                    # monta registro para salvar
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
                        "Clareza_objetivos": g1,
                        "Metodologia": g2,
                        "Qualidade_resultados": g3,
                        "Relevancia_originalidade": g4,
                        "Apresentacao_defesa": g5,
                        "Observacoes": obs
                    }

                    # salva (append) em avaliacoes.xlsx
                    if EVAL_FILE.exists():
                        try:
                            df_old = pd.read_excel(EVAL_FILE, sheet_name=EVAL_SHEET)
                        except Exception:
                            df_old = pd.DataFrame()
                        df_new = pd.concat([df_old, pd.DataFrame([record])], ignore_index=True)
                    else:
                        df_new = pd.DataFrame([record])

                    with pd.ExcelWriter(EVAL_FILE, engine="openpyxl") as writer:
                        df_new.to_excel(writer, index=False, sheet_name=EVAL_SHEET)

                    st.success("✅ Avaliação salva em 'avaliacoes.xlsx' (aba 'Respostas').")
                    # botão para baixar a planilha
                    buf_x = io.BytesIO()
                    with pd.ExcelWriter(buf_x, engine="openpyxl") as writer:
                        df_new.to_excel(writer, index=False, sheet_name=EVAL_SHEET)
                    st.download_button("⬇️ Baixar avaliações (Excel)", buf_x.getvalue(), "avaliacoes.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.error("Avaliador não encontrado nos dados atuais.")
