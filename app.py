# app.py — Lê planilha Excel (SICT/SPG) e gera QRs por avaliador

import io
import re
import json
import zipfile
from pathlib import Path
import unicodedata

import pandas as pd
import streamlit as st
import qrcode
from PIL import Image

# =====================================
# Configuração da página
# =====================================
st.set_page_config(
    page_title="Avaliadores - SICT/SPG",
    page_icon="🧾",
    layout="wide"
)

st.title("🧾 Trabalho por avaliador - SICT / SPG")
#st.markdown(
#    """
#    **Como usar**
#    1) Envie a planilha **.xlsx** com as abas (`GERAL`, `SICT` e/ou `SPG`).  
#    2) Selecione a **aba** e confira os avaliadores detectados.  
#    3) Escolha um avaliador para ver suas atribuições e **gerar o QR menor**.  
#    4) (Opcional) Gere **todos os QRs** em lote (.zip).
#
#    > Dica: sem upload, o app tenta carregar `cronograma.xlsx` ou `cronograma_SICT_SPG.xlsx` da raiz do repositório.
#    """
#)

# =====================================
# Utilitários
# =====================================
NBSP = "\xa0"

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

def strip_accents(text: str) -> str:
    text = unicodedata.normalize('NFKD', str(text))
    return ''.join(ch for ch in text if not unicodedata.combining(ch))

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
    """Escolhe a melhor coluna do DataFrame para o rótulo padrão."""
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
    # limpeza básica de cabeçalhos
    df = df.copy()
    df.columns = [clean_cell(c) for c in df.columns]

    # renomeia para padrão
    rename_map = {}
    for std_name, options in EXPECTED_COLS.items():
        col = find_column(options, list(df.columns))
        if col:
            rename_map[col] = std_name
    df = df.rename(columns=rename_map)

    # garante todas as esperadas
    for k in EXPECTED_COLS.keys():
        if k not in df.columns:
            df[k] = ""

    # mantém apenas as esperadas, na ordem
    df = df[list(EXPECTED_COLS.keys())].copy()

    # limpeza de valores
    for c in df.columns:
        df[c] = df[c].astype(str).map(clean_cell)

    return df

def tidy_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Limpa/normaliza dados, preenche células 'mescladas' (ffill) e padroniza subevento."""
    if df.empty:
        return df

    # Forward-fill (se vierem vazias de origem)
    ffill_cols = ["Subevento", "Áreas", "Dia", "Hora"]
    for c in ffill_cols:
        df[c] = df[c].replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    df[ffill_cols] = df[ffill_cols].ffill()

    # Normaliza painel
    df["Nº do Painel"] = (
        df["Nº do Painel"]
        .astype(str)
        .str.replace(r"[^\dA-Za-z\-/. ]+", "", regex=True)
        .str.strip()
    )

    # Normaliza Subevento (XIV SICT / XII SPG)
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
    """Cria dicionário {avaliador: [lista de trabalhos]} a partir das colunas AVALIADOR 1/2."""
    eval_map = {}
    if df.empty:
        return eval_map

    def push(name, row):
        name = clean_cell(name)
        if not name:
            return
        eval_map.setdefault(name, []).append({
            "aluno": row.get("Aluno(a)", ""),
            "titulo": row.get("Título", ""),
            "area": row.get("Áreas", ""),
            "painel": row.get("Nº do Painel", ""),
            "subevento": row.get("Subevento", ""),
            "dia": row.get("Dia", ""),
            "hora": row.get("Hora", "")
        })

    for _, row in df.iterrows():
        push(row.get("AVALIADOR 1", ""), row)
        push(row.get("AVALIADOR 2", ""), row)

    # Ordena por subevento, dia, hora, painel
    for k in list(eval_map.keys()):
        eval_map[k] = sorted(
            eval_map[k],
            key=lambda r: (r.get("subevento",""), r.get("dia",""), r.get("hora",""), r.get("painel",""))
        )

    return eval_map

def make_qr_image(
    payload_obj,
    box_size: int = 6,
    border: int = 2,
    prefer_q=True,
    mini=False
) -> Image.Image:
    """
    Gera QR com fallback:
    - Compacta JSON (sem espaços)
    - Tenta ERROR_CORRECT_Q, depois M, depois L
    - Se ainda falhar, remove 'titulo' de cada trabalho (mini=True) e tenta de novo
    """
    # 1) compacta JSON (menor)
    def to_text(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))

    def try_build(txt, err_level):
        qr = qrcode.QRCode(
            version=1,  # começa pequeno; fit=True aumenta se necessário
            error_correction=err_level,
            box_size=box_size,
            border=border
        )
        qr.add_data(txt)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # monta payload base
    payload = payload_obj

    # se mini, remova títulos para encurtar
    if mini and isinstance(payload, dict) and "trabalhos" in payload:
        slim = []
        for r in payload["trabalhos"]:
            slim.append({
                "aluno": r.get("aluno", ""),
                "area": r.get("area", ""),
                "painel": r.get("painel", ""),
                "subevento": r.get("subevento", ""),
                "dia": r.get("dia", ""),
                "hora": r.get("hora", "")
            })
        payload = {**payload, "trabalhos": slim}

    txt = to_text(payload)

    # Ordem de tentativas de correção
    levels = [
        qrcode.constants.ERROR_CORRECT_Q,
        qrcode.constants.ERROR_CORRECT_M,
        qrcode.constants.ERROR_CORRECT_L,
    ]
    if not prefer_q:
        levels = [
            qrcode.constants.ERROR_CORRECT_M,
            qrcode.constants.ERROR_CORRECT_L,
            qrcode.constants.ERROR_CORRECT_Q,
        ]

    # Tenta com o payload atual
    last_err = None
    for lvl in levels:
        try:
            return try_build(txt, lvl)
        except Exception as e:
            last_err = e

    # Fallback: ativa modo "mini" (sem títulos) e tenta de novo
    if not mini:
        return make_qr_image(payload_obj, box_size=box_size, border=border, prefer_q=False, mini=True)

    # Se ainda falhar, propaga um erro claro
    raise RuntimeError(
        f"QR muito grande mesmo no modo compacto. Tente reduzir conteúdos ou gerar QRs separados. Erro: {last_err}"
    )

def df_for_evaluator(name: str, eval_map: dict) -> pd.DataFrame:
    rows = eval_map.get(name, [])
    if not rows:
        return pd.DataFrame(columns=["Aluno(a)", "Título", "Nº do Painel", "Área", "Subevento", "Dia", "Hora"])
    return pd.DataFrame([
        {
            "Aluno(a)": r["aluno"],
            "Título": r["titulo"],
            "Nº do Painel": r["painel"],
            "Área": r["area"],
            "Subevento": r["subevento"],
            "Dia": r["dia"],
            "Hora": r["hora"]
        } for r in rows
    ])

def badge_evento(subs: list[str]) -> str:
    """Badges de Subevento(s) para validação visual."""
    if not subs:
        return ""
    pills = []
    for s in subs:
        color = "#2563EB" if "SICT" in s.upper() else "#047857" if "SPG" in s.upper() else "#374151"
        pills.append(
            f"""<span style="
                display:inline-block;
                padding:4px 10px;
                margin:0 6px 6px 0;
                border-radius:9999px;
                background:{color};
                color:white;
                font-size:12px;
                font-weight:600;
                ">{s}</span>"""
        )
    return "<div>" + "".join(pills) + "</div>"

# =====================================
# Entrada do arquivo (Excel)
# =====================================
st.sidebar.header("📄 Planilha do Cronograma")
uploaded = st.sidebar.file_uploader("Envie o arquivo .xlsx", type=["xlsx"])

df_dict = {}
sheet_name = None

if uploaded is not None:
    with st.spinner("Lendo planilha enviada..."):
        xls = pd.ExcelFile(uploaded)
        df_dict = {name: pd.read_excel(xls, sheet_name=name) for name in xls.sheet_names}
else:
    # tenta arquivos padrão na raiz
    candidates = [Path("cronograma.xlsx"), Path("cronograma_SICT_SPG.xlsx")]
    found = None
    for p in candidates:
        if p.exists():
            found = p
            break
    if found:
        with st.spinner(f"Lendo planilha local: {found.name}"):
            xls = pd.ExcelFile(found)
            df_dict = {name: pd.read_excel(xls, sheet_name=name) for name in xls.sheet_names}

if not df_dict:
    st.info("Envie uma **planilha .xlsx** ou inclua `cronograma.xlsx` / `cronograma_SICT_SPG.xlsx` no repositório.")
    st.stop()

# Seletor de aba
sheet_options = list(df_dict.keys())
default_sheet = "SICT" if "SICT" in sheet_options else ("SPG" if "SPG" in sheet_options else sheet_options[0])
sheet_name = st.sidebar.selectbox("Aba da planilha", options=sheet_options, index=sheet_options.index(default_sheet))

# Normaliza DataFrame selecionado
raw_df = df_dict[sheet_name]
norm_df = ensure_expected_columns(raw_df)
df = tidy_dataframe(norm_df)

# Mapeia avaliadores
eval_map = build_mapping_by_evaluator(df)
all_evals = sorted([e for e in eval_map.keys() if e])

if not all_evals:
    st.warning("Nenhum avaliador encontrado nas colunas 'AVALIADOR 1'/'AVALIADOR 2' da aba selecionada.")
    st.stop()

# =====================================
# UI principal
# =====================================
st.subheader(f"🎯 Selecione um Avaliador(a): **{sheet_name}**")
c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    selected_eval = st.selectbox("Avaliador(a)", options=[""] + all_evals, index=0)
with c2:
    box = st.number_input("Tamanho do QR (box_size)", min_value=4, max_value=12, value=6, step=1)
with c3:
    border = st.number_input("Borda do QR (pixels)", min_value=1, max_value=6, value=2, step=1)

if selected_eval:
    df_show = df_for_evaluator(selected_eval, eval_map)
    st.markdown(f"**Atribuições de:** {selected_eval}")

    subevents = sorted({s for s in df_show["Subevento"].unique().tolist() if s})
    if subevents:
        st.markdown("**Subevento(s):**")
        st.markdown(badge_evento(subevents), unsafe_allow_html=True)

    st.dataframe(df_show, use_container_width=True)

    payload = {
        "avaliador": selected_eval,
        "quantidade_trabalhos": len(eval_map[selected_eval]),
        "trabalhos": eval_map[selected_eval]
    }

    img = make_qr_image(payload, box_size=int(box), border=int(border))
    st.image(img, caption=f"QR do Avaliador(a): {selected_eval}")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    st.download_button(
        label="⬇️ Baixar QR (PNG)",
        data=buf.getvalue(),
        file_name=f"QR_{selected_eval.replace(' ', '_')}.png",
        mime="image/png"
    )

    # CSV das atribuições
    csv_buf = io.StringIO()
    df_show.to_csv(csv_buf, index=False)
    st.download_button(
        label="⬇️ Baixar atribuições (CSV)",
        data=csv_buf.getvalue().encode("utf-8"),
        file_name=f"atribuicoes_{selected_eval.replace(' ', '_')}.csv",
        mime="text/csv"
    )

st.divider()

# =====================================
# Lote: todos os QRs
# =====================================
st.subheader("📦 Gerar QRs de **todos** os avaliadores?")
cz1, cz2, cz3 = st.columns([1,1,1])
with cz1:
    do_zip = st.checkbox("Preparar .zip com todos os QRs")
with cz2:
    box_zip = st.number_input("Tamanho do QR (lote)", min_value=4, max_value=12, value=6, step=1, key="box_zip")
with cz3:
    border_zip = st.number_input("Borda do QR (lote)", min_value=1, max_value=6, value=2, step=1, key="border_zip")

if do_zip:
    with st.spinner("Gerando QRs (lote)..."):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in all_evals:
                payload = {
                    "avaliador": name,
                    "quantidade_trabalhos": len(eval_map[name]),
                    "trabalhos": eval_map[name]
                }
                txt = json.dumps(payload, ensure_ascii=False, indent=2)
                img = make_qr_image(txt, box_size=int(box_zip), border=int(border_zip))

                img_bytes = io.BytesIO()
                img.save(img_bytes, format="PNG")
                img_bytes.seek(0)

                filename = f"QR_{name.replace(' ', '_')}.png"
                zf.writestr(filename, img_bytes.read())

        st.download_button(
            label="⬇️ Baixar ZIP com todos os QRs",
            data=zip_buffer.getvalue(),
            file_name=f"QRs_Avaliadores_{sheet_name}.zip",
            mime="application/zip"
        )

st.caption("Pronto para uso no Streamlit Cloud ou localmente com `streamlit run app.py`.")
