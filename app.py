# app.py
import io
import re
import json
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st
from docx import Document
from unidecode import unidecode
import qrcode
from PIL import Image

# =====================================
# Configuração básica da página
# =====================================
st.set_page_config(
    page_title="QR dos Avaliadores - SICT/SPG",
    page_icon="🧾",
    layout="wide"
)

st.title("🧾 Geração de QR Codes por Avaliador (SICT / SPG)")
st.markdown(
    """
    **Como usar**
    1) Envie o arquivo **.docx** do cronograma (o mesmo que você tem).  
    2) O app extrai os trabalhos e lista os avaliadores encontrados.  
    3) Selecione um avaliador para visualizar suas atribuições e gerar o **QR**.  
    4) (Opcional) Gere **QRs de todos os avaliadores** em lote e baixe um **.zip**.

    > Dica: se preferir, coloque o arquivo no repositório com o nome `cronograma.docx`. O app tentará carregá-lo automaticamente.
    """
)

# =====================================
# Utilitários e configuração
# =====================================
NBSP = "\xa0"

EXPECTED_COLS = {
    "aluno": ["aluno", "aluno(a)", "aluna(o)"],
    "orientador": ["orientador", "orientador(a)"],
    "areas": ["áreas", "areas", "area", "área"],
    "titulo": ["título", "titulo"],
    "avaliador1": ["avaliador 1", "avaliador1", "avaliador(a) 1"],
    "avaliador2": ["avaliador 2", "avaliador2", "avaliador(a) 2"],
    "painel": ["nº do painel", "no do painel", "n do painel", "painel", "nº painel", "nº de painel"],
    "subevento": ["subevento", "evento", "sub-evento", "evento/subevento"],
    "dia": ["dia"],
    "hora": ["hora"]
}

def clean_cell(s: str) -> str:
    """Limpa espaços NBSP, quebras e múltiplos espaços."""
    if s is None:
        return ""
    s = str(s).replace(NBSP, " ")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def normalize(s: str) -> str:
    return clean_cell(s)

def col_key(name: str) -> str:
    return unidecode(clean_cell(name).lower())

def find_column(target_keys, columns):
    """Encontra a coluna cujo nome corresponde (aproximadamente) à lista de possíveis nomes."""
    colmap = {col_key(c): c for c in columns}
    for t in target_keys:
        k = col_key(t)
        if k in colmap:
            return colmap[k]
    # fallback: aproximação por prefixo
    for c in columns:
        if any(col_key(c).startswith(col_key(t)) for t in target_keys):
            return c
    return None

def read_docx_tables_to_df(file_like) -> pd.DataFrame:
    """Lê todas as tabelas do .docx e tenta montar um DataFrame consolidado com as colunas esperadas."""
    doc = Document(file_like)
    frames = []

    for tbl in doc.tables:
        matrix = []
        for r in tbl.rows:
            matrix.append([normalize(c.text) for c in r.cells])
        if not matrix:
            continue

        header = [normalize(h) for h in matrix[0]]
        data = matrix[1:]
        if len(header) < 3 or not data:
            continue

        df = pd.DataFrame(data, columns=header)

        # pontua se é uma tabela "relevante"
        header_keys = [col_key(c) for c in header]
        score = sum(
            any(col_key(opt) in header_keys for opt in opts)
            for opts in EXPECTED_COLS.values()
        )
        if score >= 5:
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    big = pd.concat(frames, ignore_index=True)

    # mapeia nomes flexíveis -> padrão interno
    mapped = {}
    for std_name, options in EXPECTED_COLS.items():
        col = find_column(options, list(big.columns))
        if col:
            mapped[std_name] = col

    big = big.rename(columns={v: k for k, v in mapped.items()})

    # garante presença das chaves esperadas (mesmo que vazias)
    for k in EXPECTED_COLS.keys():
        if k not in big.columns:
            big[k] = ""

    # limpeza básica de todas as colunas como string
    for c in big.columns:
        big[c] = big[c].astype(str).map(clean_cell)

    return big[list(EXPECTED_COLS.keys())].copy()

def tidy_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Limpa/normaliza dados, preenche células mescladas (forward fill) e padroniza subevento."""
    if df.empty:
        return df

    # Converte vazios para NaN e faz forward-fill em campos "hierárquicos"
    ffill_cols = ["subevento", "areas", "dia", "hora"]
    for c in ffill_cols:
        if c in df.columns:
            df[c] = df[c].replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    df[ffill_cols] = df[ffill_cols].ffill()

    # Normalizações pontuais
    if "painel" in df.columns:
        df["painel"] = (
            df["painel"]
            .astype(str)
            .str.replace(r"[^\dA-Za-z\-/. ]+", "", regex=True)
            .str.strip()
        )

    # Normaliza Subevento (captura “XIV SICT”, “XII SPG” etc.)
    if "subevento" in df.columns:
        def norm_event(x: str) -> str:
            s = x.upper().strip()
            s = re.sub(r"\s+", " ", s)
            m = re.search(r"\b([IVXLCDM]+)\s+(SICT|SPG)\b", s)
            if m:
                return f"{m.group(1)} {m.group(2)}"
            # fallback: só a sigla, se existir
            if "SICT" in s:
                return "SICT" if not re.search(r"\bSICT\b", s) else s
            if "SPG" in s:
                return "SPG" if not re.search(r"\bSPG\b", s) else s
            return s
        df["subevento"] = df["subevento"].map(norm_event)

    # Ajusta títulos (exibição)
    if "titulo" in df.columns:
        df["titulo"] = df["titulo"].str.replace("\n", " ").str.strip()

    # Garante string final e remove duplicatas
    for c in df.columns:
        df[c] = df[c].astype(str).map(clean_cell)
    df = df.drop_duplicates()

    return df

def build_mapping_by_evaluator(df: pd.DataFrame) -> dict:
    """Cria dicionário {avaliador: [lista de trabalhos]}."""
    eval_map = {}
    if df.empty:
        return eval_map

    def push(name, row):
        name = normalize(name)
        if not name:
            return
        eval_map.setdefault(name, []).append({
            "aluno": row.get("aluno", ""),
            "titulo": row.get("titulo", ""),
            "area": row.get("areas", ""),
            "painel": row.get("painel", ""),
            "subevento": row.get("subevento", ""),
            "dia": row.get("dia", ""),
            "hora": row.get("hora", "")
        })

    for _, row in df.iterrows():
        push(row.get("avaliador1", ""), row)
        push(row.get("avaliador2", ""), row)

    # Ordena trabalhos por subevento, dia, hora, painel
    for k in list(eval_map.keys()):
        eval_map[k] = sorted(
            eval_map[k],
            key=lambda r: (
                r.get("subevento", ""),
                r.get("dia", ""),
                r.get("hora", ""),
                r.get("painel", "")
            )
        )

    return eval_map

def make_qr_image(payload_text: str, box_size: int = 10, border: int = 4) -> Image.Image:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=box_size,
        border=border
    )
    qr.add_data(payload_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img

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
    """Gera badges em HTML simples para Subevento(s)."""
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
# Entrada do arquivo
# =====================================
st.sidebar.header("📄 Arquivo do Cronograma")
uploaded = st.sidebar.file_uploader("Envie o arquivo .docx (cronograma)", type=["docx"])

default_path = Path("cronograma.docx")
file_like = None
if uploaded is not None:
    file_like = uploaded
elif default_path.exists():
    file_like = str(default_path)

if not file_like:
    st.info("Envie o **.docx** com o cronograma para continuar (ou inclua `cronograma.docx` no repositório).")
    st.stop()

with st.spinner("Lendo e processando o documento..."):
    df_raw = read_docx_tables_to_df(file_like)
    df = tidy_dataframe(df_raw)
    if df.empty:
        st.error("Não consegui encontrar tabelas com as colunas esperadas no .docx. Verifique o arquivo.")
        st.stop()

    eval_map = build_mapping_by_evaluator(df)
    all_evals = sorted([e for e in eval_map.keys() if e])

if not all_evals:
    st.warning("Nenhum avaliador encontrado nas colunas 'AVALIADOR 1'/'AVALIADOR 2'.")
    st.stop()

# =====================================
# UI principal
# =====================================
st.subheader("🎯 Selecione um Avaliador(a) para gerar o QR")
col1, col2 = st.columns([2, 1])
with col1:
    selected_eval = st.selectbox("Avaliador(a)", options=[""] + all_evals, index=0)
with col2:
    box = st.number_input("Tamanho do QR (box_size)", min_value=4, max_value=20, value=10, step=1)

if selected_eval:
    df_show = df_for_evaluator(selected_eval, eval_map)
    st.markdown(f"**Atribuições de:** {selected_eval}")

    # Badge(s) de subevento
    subevents = sorted({s for s in df_show["Subevento"].unique().tolist() if s})
    if subevents:
        st.markdown("**Subevento(s):**", help="Detectado a partir da coluna 'Subevento' do documento.")
        st.markdown(badge_evento(subevents), unsafe_allow_html=True)

    st.dataframe(df_show, use_container_width=True)

    # Monta o payload do QR como JSON legível
    payload = {
        "avaliador": selected_eval,
        "quantidade_trabalhos": len(eval_map[selected_eval]),
        "trabalhos": eval_map[selected_eval]
    }
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)

    # Gera QR
    img = make_qr_image(payload_text, box_size=int(box))

    # Mostra e disponibiliza para download
    st.image(img, caption=f"QR do Avaliador(a): {selected_eval}")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    st.download_button(
        label="⬇️ Baixar QR (PNG)",
        data=buf.getvalue(),
        file_name=f"QR_{unidecode(selected_eval).replace(' ', '_')}.png",
        mime="image/png"
    )

    # Extra: exportar CSV das atribuições do avaliador
    csv_buf = io.StringIO()
    df_show.to_csv(csv_buf, index=False)
    st.download_button(
        label="⬇️ Baixar atribuições (CSV)",
        data=csv_buf.getvalue().encode("utf-8"),
        file_name=f"atribuicoes_{unidecode(selected_eval).replace(' ', '_')}.csv",
        mime="text/csv"
    )

st.divider()

# =====================================
# Lote: gerar todos os QRs
# =====================================
st.subheader("📦 Gerar QRs de **todos** os avaliadores (ZIP)")
colz1, colz2 = st.columns([1, 1])
with colz1:
    do_zip = st.checkbox("Preparar arquivo .zip com todos os QRs")
with colz2:
    box_zip = st.number_input("Tamanho do QR (lote)", min_value=4, max_value=20, value=8, step=1, key="box_zip")

if do_zip:
    with st.spinner("Gerando QRs..."):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in all_evals:
                payload = {
                    "avaliador": name,
                    "quantidade_trabalhos": len(eval_map[name]),
                    "trabalhos": eval_map[name]
                }
                txt = json.dumps(payload, ensure_ascii=False, indent=2)
                img = make_qr_image(txt, box_size=int(box_zip))

                img_bytes = io.BytesIO()
                img.save(img_bytes, format="PNG")
                img_bytes.seek(0)

                filename = f"QR_{unidecode(name).replace(' ', '_')}.png"
                zf.writestr(filename, img_bytes.read())

        st.download_button(
            label="⬇️ Baixar ZIP com todos os QRs",
            data=zip_buffer.getvalue(),
            file_name="QRs_Avaliadores.zip",
            mime="application/zip"
        )

st.caption("Pronto para uso no Streamlit Cloud ou localmente com `streamlit run app.py`.")
