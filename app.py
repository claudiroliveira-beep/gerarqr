#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Sep  2 13:44:52 2025

@author: dpeuser
"""

import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st
from docx import Document
from unidecode import unidecode
import qrcode
from PIL import Image

# ==============================
# Configuração básica da página
# ==============================
st.set_page_config(
    page_title="QR dos Avaliadores - SICT/SPG",
    page_icon="🧾",
    layout="wide"
)

st.title("🧾 Geração de QR Codes por Avaliador (SICT / SPG)")
st.markdown(
    """
    **Como usar**
    1) Envie o arquivo **.docx** do cronograma (o mesmo que você anexou aqui).  
    2) O app extrai os trabalhos e mostra a lista de avaliadores.  
    3) Selecione o avaliador e gere o **QR** com todas as suas atribuições.  
    4) (Opcional) Gere **QRs de todos os avaliadores** em lote e baixe um **.zip**.

    > Dica: se preferir, coloque o arquivo no repositório com o nome `cronograma.docx`. O app tentará carregá-lo automaticamente.
    """
)

# ==============================
# Utilitários
# ==============================
EXPECTED_COLS = {
    "aluno": ["aluno", "aluno(a)", "aluna(o)"],
    "orientador": ["orientador", "orientador(a)"],
    "areas": ["áreas", "areas", "area"],
    "titulo": ["título", "titulo"],
    "avaliador1": ["avaliador 1", "avaliador1", "avaliador(a) 1"],
    "avaliador2": ["avaliador 2", "avaliador2", "avaliador(a) 2"],
    "painel": ["nº do painel", "no do painel", "n do painel", "painel", "nº painel", "nº de painel"],
    "subevento": ["subevento", "evento"],
    "dia": ["dia"],
    "hora": ["hora"]
}

def normalize(s: str) -> str:
    if s is None:
        return ""
    s = str(s).strip()
    return s

def col_key(name: str) -> str:
    """Normaliza nome de coluna para comparação flexível."""
    return unidecode(name.lower().strip())

def find_column(target_keys, columns):
    """Encontra a coluna cujo nome corresponde (aproximadamente) à lista de possíveis nomes."""
    colmap = {col_key(c): c for c in columns}
    for t in target_keys:
        k = col_key(t)
        if k in colmap:
            return colmap[k]
    # fallback: tentativa por "starts with"
    for c in columns:
        if any(col_key(c).startswith(col_key(t)) for t in target_keys):
            return c
    return None

def read_docx_tables_to_df(file_like) -> pd.DataFrame:
    """Lê todas as tabelas do .docx e tenta montar um DataFrame consolidado com as colunas esperadas."""
    doc = Document(file_like)
    frames = []

    for tbl in doc.tables:
        # extrai matriz
        rows = []
        for r in tbl.rows:
            rows.append([normalize(c.text) for c in r.cells])

        if not rows:
            continue

        # tenta usar a primeira linha como cabeçalho
        header = [normalize(h) for h in rows[0]]
        data = rows[1:]

        # evita tabelas "ruins" sem dados
        if len(header) < 3 or len(data) == 0:
            continue

        df = pd.DataFrame(data, columns=header)

        # mantém apenas tabelas que têm um subconjunto relevante
        header_keys = [col_key(c) for c in header]
        score = sum(
            any(col_key(opt) in header_keys for opt in opts)
            for opts in EXPECTED_COLS.values()
        )
        # limiar: tem pelo menos 5 colunas mapeáveis
        if score >= 5:
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    big = pd.concat(frames, ignore_index=True)

    # Mapear nomes de colunas para padrão interno
    mapped = {}
    for std_name, options in EXPECTED_COLS.items():
        col = find_column(options, list(big.columns))
        if col:
            mapped[std_name] = col

    # Seleciona apenas colunas encontradas
    big = big.rename(columns={v: k for k, v in mapped.items()})
    return big[list(mapped.keys())].copy()

def tidy_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Limpezas e padronizações leves."""
    if df.empty:
        return df

    # Padroniza números de painel
    if "painel" in df.columns:
        df["painel"] = df["painel"].astype(str).str.strip()

    # Padroniza subevento (SICT/SPG)
    if "subevento" in df.columns:
        df["subevento"] = df["subevento"].str.upper().str.strip()

    # Remove linhas totalmente vazias
    df = df.dropna(how="all")
    # Remove duplicatas exatas
    df = df.drop_duplicates()

    # Ajusta títulos muito longos (apenas para exibição)
    if "titulo" in df.columns:
        df["titulo"] = df["titulo"].str.replace("\n", " ").str.strip()

    # Garante strings
    for c in df.columns:
        df[c] = df[c].astype(str).fillna("").str.strip()

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

# ==============================
# Entrada do arquivo
# ==============================
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

# ==============================
# UI principal
# ==============================
st.subheader("🎯 Selecione um Avaliador(a) para gerar o QR")
col1, col2 = st.columns([2, 1])
with col1:
    selected_eval = st.selectbox("Avaliador(a)", options=[""] + all_evals, index=0)
with col2:
    box = st.number_input("Tamanho do QR (box_size)", min_value=4, max_value=20, value=10, step=1)

if selected_eval:
    df_show = df_for_evaluator(selected_eval, eval_map)
    st.markdown(f"**Atribuições de:** {selected_eval}")
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

st.divider()

# ==============================
# Lote: gerar todos os QRs
# ==============================
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
